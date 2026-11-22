"""Clients for the NoLongerEvil APIs.

LocalClient  - the self-hosted control API (Docker image, HA add-on, or the
               plugin's embedded server). No auth, SSE push via /api/events.
HostedClient - the nolongerevil.com v1 API. Bearer `nle_` key, 20 req/min,
               returns raw Nest buckets which we normalise to the same shape
               LocalClient gets from /status.

Both return status dicts shaped like the self-hosted server's /status
response, so the plugin only has one mapping to Indigo states.
"""

import json
import os
import socket
import threading
import time
from collections import deque

import requests

USER_AGENT = "Indigo-NoLongerEvil"


def _find_socket(resp):
    """Dig the socket out of a streaming requests response (urllib3 1.x/2.x)."""
    for path in (("_fp", "fp", "raw", "_sock"), ("_connection", "sock")):
        obj = resp.raw
        try:
            for attr in path:
                obj = getattr(obj, attr)
        except AttributeError:
            continue
        if isinstance(obj, socket.socket):
            return obj
    return None


class NLEError(Exception):
    pass


class NLEUnsupported(NLEError):
    """The selected connection type can't do this."""


# --------------------------------------------------------------------- local
class LocalClient:
    supports_events = True
    supports_eco_temps = True
    supports_hot_water_boost = True

    def __init__(self, base_url, logger, timeout=10, db_path=None):
        self.base = base_url.rstrip("/")
        self.logger = logger
        self.timeout = timeout
        self.db_path = db_path or None
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self._sse_resp = None
        self._sse_thread = None
        self._sse_stop = threading.Event()

    # ------------------------------------------------------------- helpers
    def _get(self, path, **params):
        try:
            r = self.session.get(self.base + path, params=params or None, timeout=self.timeout)
        except requests.RequestException as exc:
            raise NLEError(f"cannot reach {self.base}: {exc.__class__.__name__}") from exc
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise NLEError(f"GET {path} -> HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    def _command(self, serial, command, value):
        body = {"serial": serial, "command": command, "value": value}
        try:
            r = self.session.post(self.base + "/command", json=body, timeout=self.timeout)
        except requests.RequestException as exc:
            raise NLEError(f"cannot reach {self.base}: {exc.__class__.__name__}") from exc
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code >= 400 or not data.get("success", False):
            raise NLEError(data.get("message") or f"HTTP {r.status_code}")
        return data.get("data")

    # ---------------------------------------------------------------- read
    def health(self):
        data = self._get("/health")
        return bool(data and data.get("status") == "ok")

    def list_devices(self):
        data = self._get("/api/devices") or {}
        return [{"serial": d["serial"], "name": d.get("name") or d["serial"]}
                for d in data.get("devices", [])]

    def all_statuses(self):
        data = self._get("/api/devices") or {}
        out = {d["serial"]: d for d in data.get("devices", [])}
        if out and self.supports_raw:
            try:
                snap = self.snapshot()
            except NLEError as exc:
                self.logger.debug(f"hot water read failed: {exc}")
            else:
                for serial, st in out.items():
                    device = snap.get(serial, {}).get(f"device.{serial}", {})
                    st["hot_water"] = extract_hot_water(device.get("value"))
        return out

    def status(self, serial):
        st = self._get("/status", serial=serial)
        if st is not None and self.supports_raw:
            # /status doesn't carry hot-water fields; read them from the buckets
            try:
                device = self.snapshot().get(serial, {}).get(f"device.{serial}", {})
                st["hot_water"] = extract_hot_water(device.get("value"))
            except NLEError as exc:
                self.logger.debug(f"hot water read failed: {exc}")
        return st

    def schedule(self, serial):
        return self._get("/api/schedule", serial=serial)

    # ------------------------------------------------------- raw buckets
    @property
    def supports_raw(self):
        return bool(self.db_path)

    def snapshot(self):
        """Every stored bucket for every serial: {serial: {object_key: {...}}}.

        The Docker control API doesn't expose raw buckets, so this reads the
        server's SQLite database (read-only) when its path has been given.
        """
        if not self.db_path:
            raise NLEUnsupported(
                "raw buckets need the server's database file - set it on the connection "
                "device (see README), or use an embedded connection"
            )
        import sqlite3

        if not os.path.exists(self.db_path):
            raise NLEError(f"database file not found: {self.db_path}")
        try:
            con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5)
            try:
                rows = con.execute(
                    "SELECT serial, object_key, object_revision, value FROM states"
                ).fetchall()
            finally:
                con.close()
        except sqlite3.Error as exc:
            raise NLEError(f"couldn't read {self.db_path}: {exc}") from exc
        out = {}
        for serial, key, rev, value in rows:
            try:
                value = json.loads(value) if value else {}
            except ValueError:
                pass
            out.setdefault(serial, {})[key] = {"object_revision": rev, "value": value}
        return out

    def raw(self, serial):
        return self.snapshot().get(serial, {})

    # ------------------------------------------------------------ commands
    def set_heat_setpoint(self, serial, temp_c):
        return self._command(serial, "set_temperature", float(temp_c))

    def set_cool_setpoint(self, serial, temp_c):
        return self._command(serial, "set_temperature", float(temp_c))

    def set_range(self, serial, low_c, high_c):
        return self._command(serial, "set_temperature", {"low": float(low_c), "high": float(high_c)})

    def set_mode(self, serial, mode):
        return self._command(serial, "set_mode", mode)

    def set_eco(self, serial, on):
        return self._command(serial, "set_away", bool(on))

    def set_eco_temps(self, serial, low_c=None, high_c=None):
        value = {}
        if low_c is not None:
            value["low"] = float(low_c)
        if high_c is not None:
            value["high"] = float(high_c)
        return self._command(serial, "set_eco_temperatures", value)

    def set_fan(self, serial, value):
        """value: 'on', 'auto', or a duration in seconds."""
        return self._command(serial, "set_fan", value)

    def set_device_setting(self, serial, fields):
        return self._command(serial, "set_device_setting", fields)

    def boost_hot_water(self, serial, minutes):
        """Experimental: the server whitelists this field and the protocol docs
        describe it as the boost end time (Unix seconds). 0 cancels."""
        end = int(time.time() + minutes * 60) if minutes > 0 else 0
        return self._command(serial, "set_device_setting", {"hot_water_boost_time_to_end": end})

    # -------------------------------------------------------------- events
    def start_events(self, on_serial, on_state):
        """Listen to /api/events. on_serial(serial) per change; on_state(ok, msg)."""
        self.stop_events()
        self._sse_stop.clear()
        self._sse_thread = threading.Thread(
            target=self._sse_loop, args=(on_serial, on_state), name="NLE-SSE", daemon=True
        )
        self._sse_thread.start()

    def stop_events(self):
        self._sse_stop.set()
        resp = self._sse_resp
        if resp is not None:
            # resp.close() from another thread deadlocks on the reader's buffer
            # lock, so shut the socket down underneath it instead.
            sock = _find_socket(resp)
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        if self._sse_thread:
            self._sse_thread.join(3)
        self._sse_thread = None

    def _sse_loop(self, on_serial, on_state):
        backoff = 2
        while not self._sse_stop.is_set():
            try:
                # The server sends nothing while idle, so a long read timeout
                # just means "quiet house" - reconnect silently.
                with requests.get(self.base + "/api/events", stream=True, timeout=(5, 600),
                                  headers={"Accept": "text/event-stream",
                                           "User-Agent": USER_AGENT}) as resp:
                    self._sse_resp = resp
                    if resp.status_code != 200:
                        raise NLEError(f"events HTTP {resp.status_code}")
                    on_state(True, "event stream connected")
                    backoff = 2
                    for line in resp.iter_lines(decode_unicode=True):
                        if self._sse_stop.is_set():
                            break
                        if not line or not line.startswith("data:"):
                            continue
                        try:
                            payload = json.loads(line[5:].strip())
                        except ValueError:
                            continue
                        if payload.get("serial"):
                            on_serial(payload["serial"])
                        elif payload.get("error"):
                            on_state(False, f"server: {payload['error']}")
                    if not self._sse_stop.is_set():
                        on_state(False, "event stream closed by server; reconnecting")
                        self._sse_stop.wait(2)
            except requests.exceptions.ReadTimeout:
                continue
            except Exception as exc:  # noqa: BLE001
                if self._sse_stop.is_set():
                    break
                on_state(False, f"event stream lost ({exc.__class__.__name__}); retry in {backoff}s")
                self._sse_stop.wait(backoff)
                backoff = min(backoff * 2, 300)
            finally:
                self._sse_resp = None


class EmbeddedClient(LocalClient):
    """LocalClient pointed at the in-process server, with raw bucket access."""

    def __init__(self, server, logger):
        super().__init__(server.control_url, logger)
        self.server = server

    @property
    def supports_raw(self):
        return True

    def snapshot(self):
        data = self.server.all_objects()
        if data is None:
            raise NLEError("embedded server not running")
        return data


# -------------------------------------------------------------------- hosted
HOSTED_BASE = "https://nolongerevil.com/api/v1"


class HostedClient:
    supports_events = False
    supports_eco_temps = False
    supports_raw = True
    supports_hot_water_boost = False

    def __init__(self, api_key, logger, base_url=HOSTED_BASE, per_minute=18, timeout=15):
        self.base = base_url.rstrip("/")
        self.logger = logger
        self.timeout = timeout
        self.per_minute = per_minute  # stay under the documented 20/min
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "User-Agent": USER_AGENT,
        })
        self._calls = deque()
        self._blocked_until = 0.0
        self._lock = threading.Lock()
        self._ids = {}  # serial -> hosted device id

    # ------------------------------------------------------------- helpers
    def budget_left(self):
        now = time.time()
        with self._lock:
            while self._calls and now - self._calls[0] > 60:
                self._calls.popleft()
            if now < self._blocked_until:
                return 0
            return self.per_minute - len(self._calls)

    def _request(self, method, path, body=None):
        if self.budget_left() <= 0:
            raise NLEError("hosted API rate limit reached - skipping until the next minute")
        with self._lock:
            self._calls.append(time.time())
        try:
            r = self.session.request(method, self.base + path, json=body, timeout=self.timeout)
        except requests.RequestException as exc:
            raise NLEError(f"cannot reach nolongerevil.com: {exc.__class__.__name__}") from exc
        if r.status_code == 429:
            self._blocked_until = time.time() + 60
            raise NLEError("hosted API returned 429 (rate limited); pausing for a minute")
        if r.status_code in (401, 403):
            raise NLEError(f"hosted API rejected the key (HTTP {r.status_code}) - check scopes read/write")
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code >= 400:
            raise NLEError(data.get("error") or f"HTTP {r.status_code}")
        return data

    def _id(self, serial):
        if serial not in self._ids:
            self.list_devices()
        if serial not in self._ids:
            raise NLEError(f"thermostat {serial} not found on this nolongerevil.com account")
        return self._ids[serial]

    def _post(self, serial, suffix, body):
        return self._request("POST", f"/thermostat/{self._id(serial)}{suffix}", body)

    # ---------------------------------------------------------------- read
    def health(self):
        self.list_devices()
        return True

    def list_devices(self):
        data = self._request("GET", "/devices")
        out = []
        for d in data.get("devices", []):
            self._ids[d["serial"]] = d["id"]
            out.append({"serial": d["serial"], "name": d.get("name") or d["serial"]})
        return out

    def raw(self, serial):
        return self._request("GET", f"/thermostat/{self._id(serial)}/status")

    def snapshot(self, serial=None):
        """Hosted can only see one thermostat's buckets per call."""
        if not serial:
            raise NLEUnsupported("hosted snapshots are per thermostat")
        payload = self.raw(serial)
        return {serial: payload.get("state", {})}

    def status(self, serial):
        return normalise_buckets(serial, self.raw(serial))

    def all_statuses(self):
        raise NLEUnsupported("hosted mode polls one device at a time")

    def schedule(self, serial):
        return self._request("GET", f"/thermostat/{self._id(serial)}/schedule")

    # ------------------------------------------------------------ commands
    def set_heat_setpoint(self, serial, temp_c):
        return self._post(serial, "/temperature", {"value": float(temp_c), "mode": "heat", "scale": "C"})

    def set_cool_setpoint(self, serial, temp_c):
        return self._post(serial, "/temperature", {"value": float(temp_c), "mode": "cool", "scale": "C"})

    def set_range(self, serial, low_c, high_c):
        return self._post(serial, "/temperature/range",
                          {"low": float(low_c), "high": float(high_c), "scale": "C"})

    def set_mode(self, serial, mode):
        return self._post(serial, "/mode", {"mode": mode})

    def set_eco(self, serial, on):
        return self._post(serial, "/away", {"away": bool(on)})

    def set_eco_temps(self, serial, low_c=None, high_c=None):
        raise NLEUnsupported("the hosted API has no eco-temperature endpoint")

    def set_fan(self, serial, value):
        if isinstance(value, (int, float)):
            return self._post(serial, "/fan", {"duration": int(value)})
        return self._post(serial, "/fan", {"mode": value})

    def set_device_setting(self, serial, fields):
        raise NLEUnsupported("the hosted API has no generic device-setting endpoint")

    def boost_hot_water(self, serial, minutes):
        raise NLEUnsupported("the hosted API has no hot water endpoint")

    def start_events(self, on_serial, on_state):
        raise NLEUnsupported("hosted mode has no event stream")

    def stop_events(self):
        pass


def extract_hot_water(device_values):
    """Pull every hot-water-ish field out of a device bucket.

    Documented (NLE protocol docs): has_hot_water_control, hot_water_mode,
    hot_water_boost_time_to_end, hot_water_away_enabled, hot_water_temperature.
    The server's write whitelist also names hot_water_active. Anything else
    containing "hot_water" is returned too so the plugin can flag it.
    Returns None when the device reports nothing hot-water related.
    """
    if not isinstance(device_values, dict):
        return None
    found = {k: v for k, v in device_values.items()
             if "hot_water" in k or "heat_link" in k or "heatlink" in k}
    return found or None


def normalise_buckets(serial, payload):
    """Turn a hosted /status payload (raw Nest buckets) into /status shape.

    Mirrors format_device_status() in the self-hosted server so the plugin
    sees the same keys whichever backend is used. Unknown fields stay None.
    """
    state = (payload or {}).get("state", {})
    dv = (state.get(f"device.{serial}") or {}).get("value", {}) or {}
    sv = (state.get(f"shared.{serial}") or {}).get("value", {}) or {}
    structure = next((v.get("value", {}) for k, v in state.items()
                      if k.startswith("structure.")), {}) or {}
    name = (payload or {}).get("device", {}).get("name") or sv.get("name") or serial

    def pick(key):
        return sv.get(key) if sv.get(key) is not None else dv.get(key)

    eco = dv.get("eco")
    fan_timeout = dv.get("fan_timer_timeout", 0) or 0
    return {
        "serial": serial,
        "name": name,
        "is_available": None,  # hosted API doesn't say; plugin infers from success
        "last_seen": None,
        "current_temperature": pick("current_temperature"),
        "target_temperature": pick("target_temperature"),
        "target_temperature_high": pick("target_temperature_high"),
        "target_temperature_low": pick("target_temperature_low"),
        "humidity": dv.get("current_humidity"),
        "mode": pick("target_temperature_type"),
        "hvac": {
            "heater": bool(sv.get("hvac_heater_state")),
            "ac": bool(sv.get("hvac_ac_state")),
            "fan": bool(sv.get("hvac_fan_state")),
            "aux_heat": bool(sv.get("hvac_aux_heater_state")),
            "emer_heat": bool(sv.get("hvac_emer_heat_state")),
        },
        "fan_timer_active": isinstance(fan_timeout, (int, float)) and fan_timeout > time.time(),
        "fan_timer_timeout": fan_timeout,
        "eco_temperatures": {"high": dv.get("away_temperature_high"),
                             "low": dv.get("away_temperature_low")},
        "is_online": dv.get("is_online", True),
        "has_leaf": bool(dv.get("leaf") or sv.get("leaf")),
        "software_version": dv.get("current_version"),
        "temperature_scale": dv.get("temperature_scale", "C"),
        "capabilities": {
            "can_heat": sv.get("can_heat", dv.get("can_heat", True)),
            "can_cool": sv.get("can_cool", dv.get("can_cool", True)),
            "has_fan": sv.get("has_fan", dv.get("has_fan", False)),
            "has_emer_heat": sv.get("has_emer_heat", dv.get("has_emer_heat", False)),
        },
        "eco_mode": eco.get("mode") if isinstance(eco, dict) else None,
        "time_to_target": dv.get("time_to_target"),
        "learning_mode": dv.get("learning_mode"),
        "backplate_temperature": dv.get("backplate_temperature"),
        "local_ip": dv.get("local_ip"),
        "mac_address": dv.get("mac_address"),
        "away": bool(structure.get("away") or structure.get("manual_eco_all")),
        "temp_lock_on": dv.get("temp_lock_on"),
        "hot_water": extract_hot_water(dv),
    }
