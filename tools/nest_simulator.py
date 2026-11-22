#!/usr/bin/env python3
"""Fake Nest Gen 2 thermostat for testing the Indigo NoLongerEvil plugin.

Speaks the same device protocol a flashed thermostat uses (Basic-auth PUTs
to /nest/transport/put and chunked long-poll subscribes to /nest/transport),
so it works against the real NLE Docker server AND the plugin's embedded
server. Nothing here is mocked on the server side - only the thermostat.

It simulates a UK-style heat-only setup: a room that warms 0.1 C per tick
while the boiler is on and cools slowly otherwise, and it obeys setpoint,
mode and eco changes pushed from the server.

--heat-link adds the hot water fields the NLE protocol docs list for a UK/EU
Heat Link (plus one made-up undocumented field, so the plugin's "please send
a capture" path can be seen) and obeys hot water boosts. The real field set
is exactly what the plugin's capture feature exists to find out.

Usage:
    python3 nest_simulator.py --server http://192.168.1.50:9543
    python3 nest_simulator.py --server http://192.168.1.50:8000 --serial 02AA01AB01234599 --name Landing
    python3 nest_simulator.py --server http://127.0.0.1:9543 --heat-link

Needs only `requests`. Ctrl-C to stop. Several copies with different
serials can run at once.
"""

import argparse
import json
import random
import threading
import time

import requests


class FakeNest:
    def __init__(self, server, serial, name, tick, heat_link=False):
        self.server = server.rstrip("/")
        self.serial = serial
        self.tick = tick
        self.http = requests.Session()
        self.http.auth = (f"d.{serial}.SIM{serial[-5:]}", "simulator-password")
        self.http.headers["X-nl-protocol-version"] = "1"
        self.session_id = f"sim{serial}"
        self.lock = threading.Lock()
        self.revs = {}  # object_key -> (revision, timestamp)
        self.device = {
            "current_humidity": 48,
            "temperature_scale": "C",
            "current_version": "5.9.4-sim",
            "local_ip": "192.168.1.250",
            "mac_address": "18b430" + serial[-6:].lower(),
            "away_temperature_low": 12.0,
            "away_temperature_high": 30.0,
            "eco": {"mode": "schedule"},
            "has_fan": False,
            "can_cool": False,
            "can_heat": True,
            "learning_mode": True,
            "time_to_target": 0,
            "backplate_temperature": 19.4,
            "is_online": True,
            "leaf": False,
            "where_id": "",
        }
        self.heat_link = heat_link
        if heat_link:
            self.device.update({
                "has_hot_water_control": True,
                "hot_water_mode": "schedule",
                "hot_water_boost_time_to_end": 0,
                "hot_water_away_enabled": False,
                "hot_water_temperature": 55.0,
                "hot_water_sim_undocumented": "demo",  # stands in for unknown fields
                "model_version": "Display-2.x (sim)",
            })
        self.shared = {
            "name": name,
            "current_temperature": 19.5,
            "target_temperature": 20.0,
            "target_temperature_type": "heat",
            "hvac_heater_state": False,
            "hvac_ac_state": False,
            "hvac_fan_state": False,
            "can_heat": True,
            "can_cool": False,
            "has_fan": False,
        }
        self.eco = False
        self.stop = threading.Event()

    # -------------------------------------------------------------- protocol
    def put(self, bucket):
        key = f"{bucket}.{self.serial}"
        values = dict(self.device if bucket == "device" else self.shared)
        rev = self.revs.get(key, (0, 0))[0]
        body = {"session": self.session_id,
                "objects": [{"object_key": key, "base_object_revision": rev, "value": values}]}
        r = self.http.post(f"{self.server}/nest/transport/put", json=body, timeout=10)
        r.raise_for_status()
        for obj in r.json().get("objects", []):
            self.revs[obj["object_key"]] = (obj["object_revision"], obj["object_timestamp"])

    def subscribe_once(self):
        objects = [{"object_key": k, "object_revision": r, "object_timestamp": t}
                   for k, (r, t) in self.revs.items()]
        body = {"chunked": True, "session": self.session_id, "objects": objects}
        with self.http.post(f"{self.server}/nest/transport", json=body, stream=True,
                            timeout=(5, 400)) as r:
            r.raise_for_status()
            raw = b""
            for chunk in r.iter_content(chunk_size=None):
                raw += chunk
                for doc in self._split_json(raw):
                    self.apply_push(doc)
                    raw = b""

    @staticmethod
    def _split_json(raw):
        text = raw.decode(errors="ignore").strip()
        if not text:
            return []
        try:
            return [json.loads(text)]
        except ValueError:
            out = []
            for line in text.splitlines():
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
            return out

    def apply_push(self, doc):
        changed_device = False
        for obj in doc.get("objects", []):
            key = obj.get("object_key", "")
            val = obj.get("value") or {}
            self.revs[key] = (obj.get("object_revision", 0), obj.get("object_timestamp", 0))
            with self.lock:
                if key == f"shared.{self.serial}":
                    for f in ("target_temperature", "target_temperature_type",
                              "target_temperature_low", "target_temperature_high"):
                        if f in val and val[f] != self.shared.get(f):
                            print(f"  <- server set {f} = {val[f]}")
                            self.shared[f] = val[f]
                elif key.startswith("structure.") and "manual_eco_all" in val:
                    eco = bool(val["manual_eco_all"])
                    if eco != self.eco:
                        print(f"  <- server set eco = {eco}")
                        self.eco = eco
                        self.device["eco"] = {"mode": "manual-eco" if eco else "schedule"}
                        changed_device = True
                elif key == f"device.{self.serial}":
                    for f, v in val.items():
                        if f in ("away_temperature_low", "away_temperature_high",
                                 "temp_lock_on", "fan_timer_timeout",
                                 "hot_water_boost_time_to_end") and self.device.get(f) != v:
                            print(f"  <- server set {f} = {v}")
                            self.device[f] = v
                            changed_device = True
        if changed_device:
            self.put("device")

    # --------------------------------------------------------------- physics
    def effective_target(self):
        if self.eco:
            return self.device["away_temperature_low"]
        return self.shared["target_temperature"]

    def physics_loop(self):
        while not self.stop.wait(self.tick):
            with self.lock:
                cur = self.shared["current_temperature"]
                heating = (self.shared["target_temperature_type"] in ("heat", "range")
                           and cur < self.effective_target() - 0.2)
                if heating:
                    cur += 0.1
                elif cur > 15.0:
                    cur -= 0.05
                cur = round(cur + random.uniform(-0.02, 0.02), 2)
                changed = heating != self.shared["hvac_heater_state"] or \
                    abs(cur - self.shared["current_temperature"]) >= 0.1
                self.shared["hvac_heater_state"] = heating
                if changed:
                    self.shared["current_temperature"] = cur
                    self.device["time_to_target"] = 600 if heating else 0  # seconds, per docs
            if self.heat_link:
                end = self.device.get("hot_water_boost_time_to_end") or 0
                if end and end <= time.time():
                    self.device["hot_water_boost_time_to_end"] = 0
                    print("  -> hot water boost finished")
                    try:
                        self.put("device")
                    except requests.RequestException:
                        pass
            if changed:
                try:
                    self.put("shared")
                    print(f"  -> {cur:.1f} C, boiler {'ON' if heating else 'off'}, "
                          f"target {self.effective_target()}")
                except requests.RequestException as exc:
                    print(f"  PUT failed: {exc}")

    # ------------------------------------------------------------------ main
    def run(self):
        print(f"Fake Nest {self.serial} -> {self.server}")
        r = self.http.get(f"{self.server}/nest/entry", timeout=10)
        print(f"  entry: HTTP {r.status_code}")
        self.put("device")
        self.put("shared")
        print("  initial state sent")
        threading.Thread(target=self.physics_loop, daemon=True).start()
        while not self.stop.is_set():
            try:
                self.subscribe_once()
            except requests.RequestException as exc:
                print(f"  subscribe error: {exc.__class__.__name__}; retrying in 5s")
                self.stop.wait(5)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--server", required=True, help="NLE device-port URL, e.g. http://192.168.1.50:9543")
    p.add_argument("--serial", default="02AA01AB01234567", help="16-char fake serial")
    p.add_argument("--name", default="Hallway (simulated)")
    p.add_argument("--tick", type=float, default=10.0, help="seconds per physics step")
    p.add_argument("--heat-link", action="store_true", help="add UK/EU Heat Link hot water fields")
    a = p.parse_args()
    nest = FakeNest(a.server, a.serial, a.name, a.tick, heat_link=a.heat_link)
    try:
        nest.run()
    except KeyboardInterrupt:
        nest.stop.set()
        print("\nstopped")


if __name__ == "__main__":
    main()
