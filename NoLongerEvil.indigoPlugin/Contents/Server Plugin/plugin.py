"""NoLongerEvil Nest - Indigo plugin for Nest Gen 1/2 thermostats running
NoLongerEvil firmware.

Three ways to reach a thermostat, each an "NLE Connection" device:
  selfhosted - the NLE Docker image / HA add-on control API (push via SSE)
  embedded   - the same server, vendored and run inside this plugin
  hosted     - the nolongerevil.com cloud API (polled, rate limited)

Thermostats are native Indigo thermostat devices bound to a connection.
"""

import json
import logging
import math
import os
import socket
import subprocess
import threading
import time
from datetime import datetime

try:
    import indigo  # noqa: F401  (injected by Indigo)
except ImportError:
    pass

from nle_capture import Capture
from nle_client import EmbeddedClient, HostedClient, LocalClient, NLEError, NLEUnsupported

CONN_TYPE = "nleConnection"
TSTAT_TYPE = "nestThermostat"
HOSTED_MAX_POLLS_PER_MIN = 12  # of the 18 we allow ourselves; the rest is for commands
HOT_WATER_KNOWN = {"has_hot_water_control", "hot_water_mode", "hot_water_boost_time_to_end",
                   "hot_water_away_enabled", "hot_water_temperature", "hot_water_active"}
CAPTURE_HINT = ("Plugins → NoLongerEvil Nest → Start Heat Link / Hot Water Capture, then "
                "submit the report as a GitHub issue")


def _dev(dev_id):
    """indigo.devices lookup that returns None instead of raising."""
    try:
        return indigo.devices[int(dev_id)]
    except (KeyError, ValueError, TypeError):
        return None


def _now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _round_half(value):
    return round(float(value) * 2) / 2


def _lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))  # TEST-NET; nothing is sent
            return s.getsockname()[0]
    except OSError:
        return ""


def _temp_ui(value):
    return "" if value is None else f"{float(value):.1f} °C"


class Conn:
    """Runtime state for one NLE Connection device."""

    def __init__(self, dev_id, kind, client, poll_secs, embedded=None):
        self.dev_id = dev_id
        self.kind = kind
        self.client = client
        self.poll_secs = poll_secs
        self.embedded = embedded
        self.next_poll = 0.0
        self.last_error = None
        self.server_count = 0


class Plugin(indigo.PluginBase):
    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs, **kwargs):
        super().__init__(pluginId, pluginDisplayName, pluginVersion, pluginPrefs, **kwargs)
        self.debug = bool(pluginPrefs.get("showDebugInfo", False))
        self._apply_log_level()
        self.conns = {}  # connection dev id -> Conn
        self.embedded = None  # (dev_id, EmbeddedServer) - only one allowed
        self._pending = set()  # (conn_id, serial) waiting for a refresh
        self._delayed = []  # (due_time, conn_id, serial)
        self._lock = threading.Lock()
        self.capture = None  # active Capture, at most one
        self._hw_noted = set()  # (dev id, field) undocumented fields already logged

    # ---------------------------------------------------------- lifecycle
    def _apply_log_level(self):
        self.indigo_log_handler.setLevel(logging.DEBUG if self.debug else logging.INFO)

    def startup(self):
        self.logger.debug("startup")

    def shutdown(self):
        if self.capture:
            self.logger.info("Plugin stopping - saving the running Heat Link capture")
            self._finish_capture()
        for conn in list(self.conns.values()):
            self._stop_conn(conn)
        if self.embedded:
            self.embedded[1].stop()
            self.embedded = None

    def closedPrefsConfigUi(self, valuesDict, userCancelled):
        if not userCancelled:
            self.debug = bool(valuesDict.get("showDebugInfo", False))
            self._apply_log_level()
            self.logger.info(f"Debug logging {'on' if self.debug else 'off'}")

    def menuToggleDebug(self):
        self.debug = not self.debug
        self.pluginPrefs["showDebugInfo"] = self.debug
        self._apply_log_level()
        self.logger.info(f"Debug logging {'on' if self.debug else 'off'}")

    # ------------------------------------------------------ device comms
    def deviceStartComm(self, dev):
        if dev.deviceTypeId == CONN_TYPE:
            dev.stateListOrDisplayStateIdChanged()
            self._start_conn(dev)
        elif dev.deviceTypeId == TSTAT_TYPE:
            dev.stateListOrDisplayStateIdChanged()
            self._ensure_tstat_props(dev)
            conn_id, serial = self._binding(dev)
            if conn_id and serial:
                self._queue(conn_id, serial)

    def deviceStopComm(self, dev):
        if dev.deviceTypeId == CONN_TYPE:
            conn = self.conns.pop(dev.id, None)
            if conn:
                self._stop_conn(conn)

    def _ensure_tstat_props(self, dev):
        """ConfigUI defaults only apply at creation, so enforce the basics."""
        props = dev.pluginProps
        wanted = {
            "NumTemperatureInputs": "1",
            "NumHumidityInputs": "1",
            "SupportsHeatSetpoint": True,
            "SupportsHvacOperationMode": True,
            "ShowCoolHeatEquipmentStateUI": True,
        }
        changed = False
        for key, value in wanted.items():
            if key not in props:
                props[key] = value
                changed = True
        for key in ("SupportsCoolSetpoint", "SupportsHvacFanMode"):
            if key not in props:
                props[key] = False
                changed = True
        if props.get("serial") and props.get("address") != props["serial"]:
            props["address"] = props["serial"]
            changed = True
        if changed:
            dev.replacePluginPropsOnServer(props)

    # ------------------------------------------------------- connections
    def _start_conn(self, dev):
        props = dev.pluginProps
        kind = props.get("connectionType", "selfhosted")
        self._conn_states(dev, status="starting", connected=False)
        try:
            if kind == "selfhosted":
                client = LocalClient(props.get("serverUrl", "").strip(), self.logger,
                                     db_path=os.path.expanduser(props.get("dbPath", "").strip()))
                poll = max(1.0, float(props.get("safetyPollMinutes", 5) or 5)) * 60
                conn = Conn(dev.id, kind, client, poll)
            elif kind == "embedded":
                conn = self._start_embedded(dev, props)
            elif kind == "hosted":
                base = props.get("hostedBaseUrl", "").strip() or "https://nolongerevil.com/api/v1"
                client = HostedClient(props.get("apiKey", "").strip(), self.logger, base_url=base)
                poll = max(30.0, float(props.get("pollSeconds", 120) or 120))
                conn = Conn(dev.id, kind, client, poll)
            else:
                raise NLEError(f"unknown connection type {kind}")
        except Exception as exc:  # noqa: BLE001
            self.logger.error(f'"{dev.name}": could not start - {exc}')
            self._conn_states(dev, status="error", connected=False, lastError=str(exc))
            dev.setErrorStateOnServer("error")
            return

        self.conns[dev.id] = conn
        if conn.client.supports_events:
            conn.client.start_events(
                lambda serial, cid=dev.id: self._queue(cid, serial),
                lambda ok, msg, cid=dev.id: self._event_state(cid, ok, msg),
            )
        else:
            self._conn_states(dev, eventStream="n/a (polled)")
        self.logger.info(f'"{dev.name}": started ({self._kind_label(kind)})')

    def _start_embedded(self, dev, props):
        from nle_embedded import EmbeddedServer

        if self.embedded and self.embedded[0] != dev.id:
            raise NLEError("only one embedded server connection is allowed")
        ip = props.get("apiOriginIp", "").strip() or _lan_ip()
        if not ip:
            raise NLEError("couldn't detect this Mac's LAN IP - enter it in the connection settings")
        device_port = int(props.get("devicePort", 9543) or 9543)
        control_port = int(props.get("controlPort", 9544) or 9544)
        level = int(props.get("serverLogLevel", logging.WARNING) or logging.WARNING)
        if self.debug and level > logging.INFO:
            level = logging.INFO
        data_dir = os.path.join(indigo.server.getInstallFolderPath(), "Preferences", "Plugins",
                                self.pluginId, "embedded-server")
        if self.embedded:
            server = self.embedded[1]
            server.set_log_level(level)
        else:
            server = EmbeddedServer(self.logger, data_dir, f"http://{ip}:{device_port}",
                                    device_port, control_port,
                                    control_lan=bool(props.get("controlLan", False)),
                                    server_log_level=level)
        server.start()
        self.embedded = (dev.id, server)
        self.logger.info(
            f'"{dev.name}": point thermostats at http://{ip}:{device_port} '
            f'(installer: Self-Hosted → NLE Server)'
        )
        poll = max(1.0, float(props.get("safetyPollMinutes", 5) or 5)) * 60
        return Conn(dev.id, "embedded", EmbeddedClient(server, self.logger), poll, embedded=server)

    def _stop_conn(self, conn):
        try:
            conn.client.stop_events()
        except Exception:  # noqa: BLE001
            pass
        if conn.embedded and self.embedded and self.embedded[0] == conn.dev_id:
            conn.embedded.stop()
            self.embedded = None

    @staticmethod
    def _kind_label(kind):
        return {"selfhosted": "self-hosted server", "embedded": "embedded server",
                "hosted": "nolongerevil.com"}.get(kind, kind)

    def _conn_states(self, dev, **states):
        current = dev.states
        updates = [{"key": k, "value": v} for k, v in states.items() if current.get(k) != v]
        if updates:
            dev.updateStatesOnServer(updates)

    def _event_state(self, conn_id, ok, msg):
        """Called from the SSE thread."""
        dev = _dev(conn_id)
        if not dev:
            return
        if ok:
            self.logger.debug(f'"{dev.name}": {msg}')
            self._conn_states(dev, eventStream="connected")
            # Anything could have changed while we were disconnected
            conn = self.conns.get(conn_id)
            if conn:
                conn.next_poll = 0
        else:
            was = dev.states.get("eventStream")
            self._conn_states(dev, eventStream="reconnecting")
            if was == "connected":
                self.logger.warning(f'"{dev.name}": {msg}')
            else:
                self.logger.debug(f'"{dev.name}": {msg}')

    def _conn_ok(self, conn, dev, count=None):
        if conn.last_error:
            self.logger.info(f'"{dev.name}": connection restored')
            conn.last_error = None
        if dev.errorState:
            dev.setErrorStateOnServer(None)
        states = {"status": "connected", "connected": True, "lastUpdate": _now_str()}
        if count is not None:
            states["thermostatCount"] = count
        self._conn_states(dev, **states)

    def _conn_error(self, conn, dev, msg):
        # Log a given error once; repeats go to debug until it clears
        if msg != conn.last_error:
            self.logger.error(f'"{dev.name}": {msg}')
        else:
            self.logger.debug(f'"{dev.name}": {msg}')
        conn.last_error = msg
        self._conn_states(dev, status="error", connected=False, lastError=msg)
        dev.setErrorStateOnServer("error")

    # -------------------------------------------------------- main loop
    def runConcurrentThread(self):
        try:
            while True:
                self._process_delayed()
                self._process_pending()
                self._capture_tick()
                now = time.time()
                for conn in list(self.conns.values()):
                    if now >= conn.next_poll:
                        conn.next_poll = now + self._poll_interval(conn)
                        self._poll(conn)
                self.sleep(0.5)
        except self.StopThread:
            pass

    def _poll_interval(self, conn):
        if conn.kind != "hosted":
            return conn.poll_secs
        n = max(1, len(self._tstats_for(conn.dev_id)))
        return max(conn.poll_secs, math.ceil(60.0 * n / HOSTED_MAX_POLLS_PER_MIN))

    def _queue(self, conn_id, serial):
        with self._lock:
            self._pending.add((conn_id, serial))

    def _queue_later(self, conn_id, serial, delay):
        with self._lock:
            self._delayed.append((time.time() + delay, conn_id, serial))

    def _process_delayed(self):
        now = time.time()
        with self._lock:
            due = [d for d in self._delayed if d[0] <= now]
            self._delayed = [d for d in self._delayed if d[0] > now]
            for _, conn_id, serial in due:
                self._pending.add((conn_id, serial))

    def _process_pending(self):
        with self._lock:
            pending, self._pending = self._pending, set()
        for conn_id, serial in pending:
            conn = self.conns.get(conn_id)
            conn_dev = _dev(conn_id)
            if not conn or not conn_dev:
                continue
            devs = [d for d in self._tstats_for(conn_id) if d.pluginProps.get("serial") == serial]
            if not devs:
                self.logger.debug(f'"{conn_dev.name}": change for {serial} (no Indigo device)')
                continue
            try:
                status = conn.client.status(serial)
            except NLEError as exc:
                self._conn_error(conn, conn_dev, str(exc))
                continue
            self._conn_ok(conn, conn_dev)
            for dev in devs:
                self._update_tstat(dev, status)

    def _poll(self, conn):
        dev = _dev(conn.dev_id)
        if not dev:
            return
        tstats = self._tstats_for(conn.dev_id)
        try:
            if conn.kind == "hosted":
                devices = conn.client.list_devices() if not conn.server_count else None
                if devices is not None:
                    conn.server_count = len(devices)
                for t in tstats:
                    serial = t.pluginProps.get("serial")
                    if serial:
                        self._update_tstat(t, conn.client.status(serial))
                self._conn_ok(conn, dev, conn.server_count)
            else:
                statuses = conn.client.all_statuses()
                conn.server_count = len(statuses)
                self._conn_ok(conn, dev, len(statuses))
                for t in tstats:
                    self._update_tstat(t, statuses.get(t.pluginProps.get("serial")))
        except NLEError as exc:
            self._conn_error(conn, dev, str(exc))
            for t in tstats:
                if t.states.get("online"):
                    t.updateStateOnServer("online", False)
                t.setErrorStateOnServer("no server")

    def _tstats_for(self, conn_id):
        return [d for d in indigo.devices.iter(f"self.{TSTAT_TYPE}")
                if d.enabled and d.pluginProps.get("connectionId") == str(conn_id)]

    def _binding(self, dev):
        props = dev.pluginProps
        try:
            conn_id = int(props.get("connectionId", 0) or 0)
        except ValueError:
            conn_id = 0
        return conn_id, props.get("serial", "")

    # ------------------------------------------------- state mapping
    def _update_tstat(self, dev, st):
        if not st:
            if dev.states.get("online") is not False:
                self.logger.warning(f'"{dev.name}": thermostat not found on its server')
            dev.updateStateOnServer("online", False)
            dev.setErrorStateOnServer("not found")
            return

        self._sync_capabilities(dev, st.get("capabilities") or {})
        mode = (st.get("mode") or "").lower()
        hvac = st.get("hvac") or {}
        target = st.get("target_temperature")
        low = st.get("target_temperature_low")
        high = st.get("target_temperature_high")
        eco = st.get("eco_temperatures") or {}
        eco_mode = st.get("eco_mode")
        available = st.get("is_available")
        online = True if available is None else bool(available)

        if mode == "range":
            heat_sp, cool_sp = low, high
        elif mode == "cool":
            heat_sp, cool_sp = None, target
        else:  # heat, emergency, off
            heat_sp, cool_sp = target, None

        hvac_mode = {
            "off": indigo.kHvacMode.Off,
            "heat": indigo.kHvacMode.Heat,
            "emergency": indigo.kHvacMode.Heat,
            "cool": indigo.kHvacMode.Cool,
            "range": indigo.kHvacMode.HeatCool,
        }.get(mode)

        updates = []

        def put(key, value, ui=None):
            if value is None:
                return
            if dev.states.get(key) != value:
                item = {"key": key, "value": value}
                if ui is not None:
                    item["uiValue"] = ui
                updates.append(item)

        cur = st.get("current_temperature")
        put("temperatureInput1", round(float(cur), 1) if cur is not None else None, _temp_ui(cur))
        if st.get("humidity") is not None:
            put("humidityInput1", float(st["humidity"]), f"{float(st['humidity']):.0f}%")
        if heat_sp is not None:
            put("setpointHeat", float(heat_sp), _temp_ui(heat_sp))
        if cool_sp is not None:
            put("setpointCool", float(cool_sp), _temp_ui(cool_sp))
        if hvac_mode is not None:
            put("hvacOperationMode", hvac_mode)
        heating = bool(hvac.get("heater") or hvac.get("aux_heat") or hvac.get("emer_heat"))
        put("hvacHeaterIsOn", heating)
        put("hvacCoolerIsOn", bool(hvac.get("ac")))
        put("hvacFanIsOn", bool(hvac.get("fan")) or bool(st.get("fan_timer_active")))
        if dev.pluginProps.get("SupportsHvacFanMode"):
            put("hvacFanMode", indigo.kFanMode.AlwaysOn if st.get("fan_timer_active")
                else indigo.kFanMode.Auto)

        # Eco: the server reports device.eco.mode ("schedule" = normal).
        # Treat anything else, or structure away, as eco.
        eco_active = bool(st.get("away")) or (eco_mode not in (None, "", "schedule"))
        put("thermostatName", str(st.get("name") or ""))
        put("online", online)
        if st.get("last_seen"):
            put("lastSeen", str(st["last_seen"]).replace("T", " ").split(".")[0])
        put("nestMode", mode)
        put("heatingActive", heating)
        put("ecoActive", eco_active)
        put("ecoMode", str(eco_mode or ""))
        if eco.get("low") is not None:
            put("ecoTempLow", float(eco["low"]), _temp_ui(eco["low"]))
        if eco.get("high") is not None:
            put("ecoTempHigh", float(eco["high"]), _temp_ui(eco["high"]))
        if target is not None:
            put("targetTemp", float(target), _temp_ui(target))
        put("timeToTarget", self._ttt(st.get("time_to_target")))
        put("leaf", bool(st.get("has_leaf")))
        if st.get("learning_mode") is not None:
            put("learningMode", bool(st["learning_mode"]))
        if st.get("backplate_temperature") is not None:
            put("backplateTemp", float(st["backplate_temperature"]),
                _temp_ui(st["backplate_temperature"]))
        put("firmware", str(st.get("software_version") or ""))
        put("ipAddress", str(st.get("local_ip") or ""))
        put("macAddress", str(st.get("mac_address") or ""))
        self._map_hot_water(dev, st.get("hot_water"), put)

        if updates:
            updates.append({"key": "lastUpdate", "value": _now_str()})
            dev.updateStatesOnServer(updates)
            if self.debug:
                changed = ", ".join(f"{u['key']}={u.get('uiValue', u['value'])}"
                                    for u in updates if u["key"] != "lastUpdate")
                self.logger.debug(f'"{dev.name}": {changed}')
        if online:
            if dev.errorState:
                dev.setErrorStateOnServer(None)
        elif dev.errorState != "offline":
            dev.setErrorStateOnServer("offline")

    @staticmethod
    def _ttt(value):
        """time_to_target: the NLE docs say seconds remaining; accept an epoch
        time too until a real thermostat confirms which. 0 means none."""
        try:
            value = float(value or 0)
        except (TypeError, ValueError):
            return ""
        if value <= 0:
            return ""
        if value > 1e9:
            value = value - time.time()
            if value <= 0:
                return ""
        end = datetime.fromtimestamp(time.time() + value)
        return f"{int(value // 60)} min (at {end:%H:%M})"

    def _map_hot_water(self, dev, hw, put):
        """Hot water states from the documented Heat Link fields.

        hw is None when the connection can't see raw buckets (Docker without
        its database file) or the thermostat reports nothing hot-water related.
        """
        if not hw:
            if dev.states.get("hotWaterSupported"):
                put("hotWaterSupported", False)
            return
        supported = bool(hw.get("has_hot_water_control")) or any(
            k != "has_hot_water_control" for k in hw)
        put("hotWaterSupported", supported)
        put("hotWaterFields", ", ".join(sorted(hw)))
        if hw.get("hot_water_mode") is not None:
            put("hotWaterMode", str(hw["hot_water_mode"]))
        if "hot_water_active" in hw:
            put("hotWaterActive", str(hw["hot_water_active"]).lower())
        if "hot_water_away_enabled" in hw:
            put("hotWaterAwayEnabled", str(hw["hot_water_away_enabled"]).lower())
        if hw.get("hot_water_temperature") is not None:
            try:
                temp = float(hw["hot_water_temperature"])
                put("hotWaterTemp", temp, _temp_ui(temp))
            except (TypeError, ValueError):
                pass
        if "hot_water_boost_time_to_end" in hw:
            try:
                end = float(hw["hot_water_boost_time_to_end"] or 0)
            except (TypeError, ValueError):
                end = 0
            active = end > time.time()
            put("hotWaterBoostActive", active)
            put("hotWaterBoostEnds", f"{datetime.fromtimestamp(end):%H:%M}" if active else "")

        # Ask for a capture: once when hot water is first seen, and once per
        # field nobody has documented yet.
        prompted = set(filter(None, self.pluginPrefs.get("hwPrompted", "").split(",")))
        if supported and dev.address not in prompted:
            prompted.add(dev.address)
            self.pluginPrefs["hwPrompted"] = ",".join(sorted(prompted))
            self.logger.info(
                f'"{dev.name}": hot water control detected. Heat Link support is still '
                f"experimental - you can help finish it: {CAPTURE_HINT}."
            )
        for field in sorted(set(hw) - HOT_WATER_KNOWN):
            if (dev.id, field) not in self._hw_noted:
                self._hw_noted.add((dev.id, field))
                self.logger.info(
                    f'"{dev.name}": undocumented hot water field "{field}" = {hw[field]!r}. '
                    f"Please send a capture: {CAPTURE_HINT}."
                )

    def _sync_capabilities(self, dev, caps):
        props = dev.pluginProps
        want_cool = bool(caps.get("can_cool", False))
        want_fan = bool(caps.get("has_fan", False))
        want_heat = bool(caps.get("can_heat", True))
        if (bool(props.get("SupportsCoolSetpoint")) != want_cool
                or bool(props.get("SupportsHvacFanMode")) != want_fan
                or bool(props.get("SupportsHeatSetpoint", True)) != want_heat):
            props["SupportsCoolSetpoint"] = want_cool
            props["SupportsHvacFanMode"] = want_fan
            props["SupportsHeatSetpoint"] = want_heat
            dev.replacePluginPropsOnServer(props)
            self.logger.info(
                f'"{dev.name}": capabilities - heat {want_heat}, cool {want_cool}, fan {want_fan}'
            )

    # ---------------------------------------------------------- commands
    def _target(self, dev):
        conn_id, serial = self._binding(dev)
        conn = self.conns.get(conn_id)
        if not conn or not serial:
            raise NLEError("its connection isn't running - check the NLE Connection device")
        return conn, serial

    def _send(self, dev, description, func, *args, optimistic=None):
        try:
            conn, serial = self._target(dev)
            func(conn.client, serial, *args)
        except NLEUnsupported as exc:
            self.logger.warning(f'"{dev.name}": can\'t {description} - {exc}')
            return False
        except NLEError as exc:
            self.logger.error(f'"{dev.name}": {description} failed - {exc}')
            return False
        self.logger.info(f'sent "{dev.name}" {description}')
        if optimistic:
            dev.updateStatesOnServer(optimistic)
        if not conn.client.supports_events:
            # No push from the cloud; check the result once the device has applied it
            self._queue_later(conn.dev_id, serial, 10)
        return True

    def actionControlThermostat(self, action, dev):
        ta = action.thermostatAction
        T = indigo.kThermostatAction
        mode = dev.states.get("nestMode", "")

        if ta == T.SetHvacMode:
            nest = {
                indigo.kHvacMode.Off: "off",
                indigo.kHvacMode.Heat: "heat",
                indigo.kHvacMode.Cool: "cool",
                indigo.kHvacMode.HeatCool: "heat-cool",
                indigo.kHvacMode.ProgramHeat: "heat",
                indigo.kHvacMode.ProgramCool: "cool",
                indigo.kHvacMode.ProgramHeatCool: "heat-cool",
            }.get(action.actionMode)
            if not nest:
                self.logger.warning(f'"{dev.name}": unsupported mode {action.actionMode}')
                return
            self._send(dev, f"mode {nest}", lambda c, s: c.set_mode(s, nest),
                       optimistic=[{"key": "hvacOperationMode", "value": action.actionMode}])

        elif ta in (T.SetHeatSetpoint, T.IncreaseHeatSetpoint, T.DecreaseHeatSetpoint):
            current = float(dev.states.get("setpointHeat", 20) or 20)
            if ta == T.SetHeatSetpoint:
                new = action.actionValue
            elif ta == T.IncreaseHeatSetpoint:
                new = current + action.actionValue
            else:
                new = current - action.actionValue
            new = _round_half(new)
            if mode == "range":
                high = float(dev.states.get("setpointCool", new + 3))
                func = lambda c, s: c.set_range(s, new, high)  # noqa: E731
            else:
                func = lambda c, s: c.set_heat_setpoint(s, new)  # noqa: E731
            self._send(dev, f"heat setpoint {new:.1f} °C", func,
                       optimistic=[{"key": "setpointHeat", "value": new, "uiValue": _temp_ui(new)}])

        elif ta in (T.SetCoolSetpoint, T.IncreaseCoolSetpoint, T.DecreaseCoolSetpoint):
            current = float(dev.states.get("setpointCool", 24) or 24)
            if ta == T.SetCoolSetpoint:
                new = action.actionValue
            elif ta == T.IncreaseCoolSetpoint:
                new = current + action.actionValue
            else:
                new = current - action.actionValue
            new = _round_half(new)
            if mode == "range":
                low = float(dev.states.get("setpointHeat", new - 3))
                func = lambda c, s: c.set_range(s, low, new)  # noqa: E731
            else:
                func = lambda c, s: c.set_cool_setpoint(s, new)  # noqa: E731
            self._send(dev, f"cool setpoint {new:.1f} °C", func,
                       optimistic=[{"key": "setpointCool", "value": new, "uiValue": _temp_ui(new)}])

        elif ta == T.SetFanMode:
            value = "on" if action.actionMode == indigo.kFanMode.AlwaysOn else "auto"
            self._send(dev, f"fan {value}", lambda c, s: c.set_fan(s, value),
                       optimistic=[{"key": "hvacFanMode", "value": action.actionMode}])

        elif ta in (T.RequestStatusAll, T.RequestMode, T.RequestEquipmentState,
                    T.RequestTemperatures, T.RequestHumidities, T.RequestDeadbands,
                    T.RequestSetpoints):
            self._refresh_dev(dev)

    def actionControlUniversal(self, action, dev):
        if action.deviceAction == indigo.kUniversalAction.RequestStatus:
            if dev.deviceTypeId == TSTAT_TYPE:
                self._refresh_dev(dev)
            elif dev.id in self.conns:
                self.conns[dev.id].next_poll = 0

    def _refresh_dev(self, dev):
        conn_id, serial = self._binding(dev)
        if conn_id and serial:
            self._queue(conn_id, serial)
            self.logger.info(f'"{dev.name}": refresh requested')

    # ------------------------------------------------------ custom actions
    def actionSetEco(self, action, dev):
        choice = action.props.get("ecoState", "on")
        if choice == "toggle":
            on = not bool(dev.states.get("ecoActive"))
        else:
            on = choice == "on"
        self._send(dev, f"eco {'on' if on else 'off'}", lambda c, s: c.set_eco(s, on),
                   optimistic=[{"key": "ecoActive", "value": on}])

    def actionSetEcoTemps(self, action, dev):
        def num(key):
            raw = str(action.props.get(key, "")).strip()
            return _round_half(raw) if raw else None

        try:
            low, high = num("ecoLow"), num("ecoHigh")
        except ValueError:
            self.logger.error(f'"{dev.name}": eco temperatures must be numbers')
            return
        if low is None and high is None:
            self.logger.warning(f'"{dev.name}": no eco temperatures given')
            return
        desc = "eco temps " + ", ".join(
            p for p in (f"low {low}" if low is not None else "",
                        f"high {high}" if high is not None else "") if p)
        self._send(dev, desc, lambda c, s: c.set_eco_temps(s, low, high))

    def actionSetRange(self, action, dev):
        try:
            low = _round_half(action.props.get("rangeLow"))
            high = _round_half(action.props.get("rangeHigh"))
        except (TypeError, ValueError):
            self.logger.error(f'"{dev.name}": range values must be numbers')
            return
        if low >= high:
            self.logger.error(f'"{dev.name}": low must be below high')
            return
        self._send(dev, f"range {low}-{high} °C", lambda c, s: c.set_range(s, low, high))

    def actionSetFanTimer(self, action, dev):
        try:
            minutes = int(float(action.props.get("minutes", 0) or 0))
        except ValueError:
            self.logger.error(f'"{dev.name}": minutes must be a number')
            return
        value = "auto" if minutes <= 0 else minutes * 60
        desc = "fan timer off" if minutes <= 0 else f"fan timer {minutes} min"
        self._send(dev, desc, lambda c, s: c.set_fan(s, value))

    def actionRefresh(self, action, dev):
        self._refresh_dev(dev)

    def actionBoostHotWater(self, action, dev):
        try:
            minutes = int(action.props.get("minutes", 30))
        except (TypeError, ValueError):
            minutes = 30
        if not dev.states.get("hotWaterSupported"):
            self.logger.warning(
                f'"{dev.name}": hot water control not reported (yet) - sending anyway')
        desc = "hot water boost cancel" if minutes <= 0 else f"hot water boost {minutes} min"
        self._send(dev, desc, lambda c, s: c.boost_hot_water(s, minutes))

    # ----------------------------------------------------------- capture
    def _snapshot(self, conn, serial):
        if conn.kind == "hosted":
            return conn.client.snapshot(serial)
        return conn.client.snapshot()

    def menuStartCapture(self, valuesDict, typeId):
        errors = indigo.Dict()
        if self.capture:
            errors["targetDevice"] = "A capture is already running - finish or cancel it first"
            return (False, valuesDict, errors)
        try:
            dev = indigo.devices[int(valuesDict.get("targetDevice", 0))]
        except (KeyError, ValueError):
            errors["targetDevice"] = "Pick a thermostat"
            return (False, valuesDict, errors)
        try:
            minutes = max(1, min(120, int(float(valuesDict.get("minutes", 20)))))
        except ValueError:
            errors["minutes"] = "Enter a number of minutes (1-120)"
            return (False, valuesDict, errors)
        try:
            conn, serial = self._target(dev)
            if not conn.client.supports_raw:
                raise NLEUnsupported(
                    "this connection can't see raw buckets. Set the server database file on "
                    "the connection device (see README) or use an embedded connection")
            first = self._snapshot(conn, serial)
        except NLEError as exc:
            errors["targetDevice"] = str(exc)
            return (False, valuesDict, errors)

        self.capture = Capture(dev, conn.kind, serial, minutes, valuesDict.get("note", ""),
                               first, self.pluginVersion)
        self.capture.next_sample = time.time() + self._capture_interval(conn)
        self.logger.info(
            f'Heat Link capture started for "{dev.name}" ({minutes} min). On the thermostat, '
            "work through the hot water controls: boost on, boost off, schedule, off, and "
            "back. After each step use Plugins → NoLongerEvil Nest → Add Capture Marker to "
            "say what you did. Finish with Finish Capture and Prepare GitHub Issue."
        )
        return True

    @staticmethod
    def _capture_interval(conn):
        return 30 if conn.kind == "hosted" else 3

    def _capture_tick(self):
        cap = self.capture
        if not cap or time.time() < cap.next_sample:
            return
        dev = _dev(cap.dev_id)
        conn = self.conns.get(self._binding(dev)[0]) if dev else None
        if not conn:
            self.logger.error("Heat Link capture stopped - its thermostat or connection went away")
            self.capture = None
            return
        cap.next_sample = time.time() + self._capture_interval(conn)
        try:
            changed = cap.add_snapshot(self._snapshot(conn, cap.serial))
            if changed:
                self.logger.debug(f"capture: {changed} field(s) changed")
        except NLEError as exc:
            self.logger.debug(f"capture sample failed: {exc}")
        if cap.remaining <= 0:
            self.logger.info("Heat Link capture time is up")
            self._finish_capture()

    def menuCaptureMarker(self, valuesDict, typeId):
        if not self.capture:
            return (False, valuesDict, {"text": "No capture is running"})
        text = valuesDict.get("text", "").strip()
        if not text:
            return (False, valuesDict, {"text": "Say what you did"})
        # Take a sample first so the change lands just before the marker
        self.capture.next_sample = 0
        self._capture_tick()
        if self.capture:
            self.capture.add_marker(text)
            self.logger.info(f'capture marker: "{text}" ({self.capture.remaining // 60} min left)')
        return True

    def menuFinishCapture(self):
        if not self.capture:
            self.logger.info("No capture is running")
            return
        self._finish_capture()

    def menuCancelCapture(self):
        if self.capture:
            self.capture = None
            self.logger.info("Heat Link capture cancelled - nothing saved")
        else:
            self.logger.info("No capture is running")

    def _finish_capture(self):
        cap, self.capture = self.capture, None
        dev = _dev(cap.dev_id)
        conn = self.conns.get(self._binding(dev)[0]) if dev else None
        if conn:
            try:
                cap.add_snapshot(self._snapshot(conn, cap.serial))
            except NLEError:
                pass
        device_bucket = (cap.last_snapshot.get(cap.serial, {})
                         .get(f"device.{cap.serial}", {}).get("value", {})) or {}
        folder = os.path.join(indigo.server.getInstallFolderPath(), "Preferences", "Plugins",
                              self.pluginId, "captures")
        try:
            path, summary, report = cap.write(folder, str(device_bucket.get("current_version", "")),
                                              str(device_bucket.get("model_version", "")))
        except OSError as exc:
            self.logger.error(f"couldn't save the capture: {exc}")
            return
        url = Capture.issue_url(report, summary)
        self.logger.info(
            f"Heat Link capture saved: {path}\n{summary}\n\n"
            "Please submit it: open the GitHub link below, check the pre-filled details, and "
            "drag the .json file into the issue. Serials, IPs, MACs, postcodes and account "
            "details have been replaced - have a quick look through it first.\n" + url
        )
        if self.pluginPrefs.get("openAfterCapture", True):
            for cmd in (["open", "-R", path], ["open", url]):
                try:
                    subprocess.run(cmd, check=False, timeout=10)
                except (OSError, subprocess.SubprocessError) as exc:
                    self.logger.debug(f"{cmd[0]} failed: {exc}")

    # ---------------------------------------------------------- menus
    def menuRefreshAll(self):
        for conn in self.conns.values():
            conn.next_poll = 0
        self.logger.info("Refreshing all connections")

    def menuListThermostats(self):
        if not self.conns:
            self.logger.info("No NLE Connection devices are running")
            return
        used = {(d.pluginProps.get("connectionId"), d.pluginProps.get("serial")): d.name
                for d in indigo.devices.iter(f"self.{TSTAT_TYPE}")}
        for conn in self.conns.values():
            cdev = _dev(conn.dev_id)
            name = cdev.name if cdev else conn.dev_id
            try:
                found = conn.client.list_devices()
            except NLEError as exc:
                self.logger.error(f'"{name}": {exc}')
                continue
            self.logger.info(f'"{name}" ({self._kind_label(conn.kind)}): {len(found)} thermostat(s)')
            for t in found:
                indigo_name = used.get((str(conn.dev_id), t["serial"]))
                suffix = f' → Indigo "{indigo_name}"' if indigo_name else " (no Indigo device yet)"
                self.logger.info(f'    {t["serial"]}  {t["name"]}{suffix}')

    def menuDumpDiagnostics(self, valuesDict, typeId):
        try:
            dev = indigo.devices[int(valuesDict.get("targetDevice", 0))]
        except (ValueError, KeyError):
            return (False, valuesDict, {"targetDevice": "Pick a thermostat"})
        try:
            conn, serial = self._target(dev)
        except NLEError as exc:
            self.logger.error(f'"{dev.name}": {exc}')
            return True
        self.logger.info(f'--- diagnostics for "{dev.name}" ({serial}, '
                         f'{self._kind_label(conn.kind)}) ---')
        try:
            status = dict(conn.client.status(serial) or {})
            if status.get("api_key"):
                status["api_key"] = "(redacted)"
            self.logger.info("status:\n" + json.dumps(status, indent=2, sort_keys=True, default=str))
        except NLEError as exc:
            self.logger.error(f"status failed - {exc}")
        if conn.client.supports_raw:
            try:
                raw = conn.client.raw(serial)
                self.logger.info("raw buckets:\n" + json.dumps(raw, indent=2, sort_keys=True,
                                                               default=str))
            except NLEError as exc:
                self.logger.error(f"raw dump failed - {exc}")
        else:
            self.logger.info("raw buckets: not available on this connection type "
                             "(use embedded mode, or DEBUG_LOGGING=true on the Docker server)")
        self.logger.info("--- end diagnostics ---")
        return True

    # ------------------------------------------------------- config UI
    def listConnections(self, filter="", valuesDict=None, typeId="", targetId=0):
        return [(str(d.id), d.name) for d in indigo.devices.iter(f"self.{CONN_TYPE}")]

    def listThermostats(self, filter="", valuesDict=None, typeId="", targetId=0):
        valuesDict = valuesDict or {}
        current = valuesDict.get("serial", "")
        try:
            conn = self.conns.get(int(valuesDict.get("connectionId", 0) or 0))
        except ValueError:
            conn = None
        if not conn:
            out = [("", "- pick a running connection first -")]
            if current:
                out.append((current, f"{current} (current)"))
            return out
        try:
            found = conn.client.list_devices()
        except NLEError as exc:
            out = [("", f"- {exc} -")]
            if current:
                out.append((current, f"{current} (current)"))
            return out
        taken = {d.pluginProps.get("serial") for d in indigo.devices.iter(f"self.{TSTAT_TYPE}")
                 if d.id != targetId and d.pluginProps.get("connectionId") == str(conn.dev_id)}
        out = []
        for t in found:
            label = f'{t["name"]} ({t["serial"]})'
            if t["serial"] in taken:
                label += " - already in Indigo"
            out.append((t["serial"], label))
        if current and current not in {s for s, _ in out}:
            out.append((current, f"{current} (not on server right now)"))
        return out or [("", "- no thermostats on this server yet -")]

    def configChanged(self, valuesDict, typeId="", devId=0):
        return valuesDict

    def validateDeviceConfigUi(self, valuesDict, typeId, devId):
        errors = indigo.Dict()
        if typeId == CONN_TYPE:
            kind = valuesDict.get("connectionType", "selfhosted")
            if kind == "selfhosted":
                url = valuesDict.get("serverUrl", "").strip()
                if not url.startswith(("http://", "https://")):
                    errors["serverUrl"] = "Enter a URL like http://192.168.1.50:8082"
                valuesDict["address"] = url.split("://")[-1]
                db = os.path.expanduser(valuesDict.get("dbPath", "").strip())
                if db and not os.path.isfile(db):
                    errors["dbPath"] = "File not found - see README for where to find it"
            elif kind == "embedded":
                for key in ("devicePort", "controlPort"):
                    try:
                        port = int(valuesDict.get(key, ""))
                        if not 1024 <= port <= 65535:
                            raise ValueError
                    except ValueError:
                        errors[key] = "Use a port between 1024 and 65535"
                if valuesDict.get("devicePort") == valuesDict.get("controlPort"):
                    errors["controlPort"] = "Must differ from the thermostat port"
                ip = valuesDict.get("apiOriginIp", "").strip()
                if ip:
                    try:
                        socket.inet_aton(ip)
                    except OSError:
                        errors["apiOriginIp"] = "Enter an IPv4 address (not a hostname)"
                for d in indigo.devices.iter(f"self.{CONN_TYPE}"):
                    if d.id != devId and d.pluginProps.get("connectionType") == "embedded":
                        errors["connectionType"] = f'"{d.name}" is already the embedded server'
                valuesDict["address"] = f"{ip or _lan_ip()}:{valuesDict.get('devicePort', '')}"
            elif kind == "hosted":
                if not valuesDict.get("apiKey", "").strip().startswith("nle_"):
                    errors["apiKey"] = "Hosted API keys start with nle_"
                try:
                    if float(valuesDict.get("pollSeconds", 0)) < 30:
                        raise ValueError
                except ValueError:
                    errors["pollSeconds"] = "Minimum 30 seconds"
                valuesDict["address"] = "nolongerevil.com"
            if kind in ("selfhosted", "embedded"):
                try:
                    if float(valuesDict.get("safetyPollMinutes", 0)) < 1:
                        raise ValueError
                except ValueError:
                    errors["safetyPollMinutes"] = "Minimum 1 minute"
        elif typeId == TSTAT_TYPE:
            if not valuesDict.get("connectionId"):
                errors["connectionId"] = "Pick a connection"
            if not valuesDict.get("serial"):
                errors["serial"] = "Pick a thermostat"
            valuesDict["address"] = valuesDict.get("serial", "")
        if errors:
            return (False, valuesDict, errors)
        return (True, valuesDict)
