#!/usr/bin/env python3
"""Mock of the nolongerevil.com v1 API, built from the published docs.

Lets you exercise the plugin's "nolongerevil.com" connection type without an
account or a thermostat. It keeps one fake heat-only thermostat, answers the
documented endpoints with the documented response shapes, applies commands,
enforces the 20 requests/minute limit, and prints every request.

    python3 mock_hosted_api.py              # listens on http://0.0.0.0:9600
    python3 mock_hosted_api.py --port 9700

Then make an NLE Connection device of type nolongerevil.com with any key
starting nle_, and set "API base URL" to http://127.0.0.1:9600/api/v1.

Needs only the Python standard library.
"""

import argparse
import json
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERIAL = "02AA01AB01234567"
DEV_ID = "dev_mock000001"
STATE = {
    "shared": {"name": "Hallway (mock cloud)", "current_temperature": 19.8,
               "target_temperature": 20.0, "target_temperature_type": "heat",
               "hvac_heater_state": True, "hvac_ac_state": False, "can_heat": True,
               "can_cool": False, "has_fan": False},
    "device": {"temperature_scale": "C", "current_humidity": 51, "away_temperature_low": 12.0,
               "away_temperature_high": 30.0, "eco": {"mode": "schedule"},
               "current_version": "5.9.4-mock", "fan_timer_timeout": 0, "leaf": False,
               "has_hot_water_control": True, "hot_water_mode": "schedule",
               "hot_water_boost_time_to_end": 0},
    "structure": {"name": "Home", "away": False, "manual_eco_all": False},
}
REV = {"shared": 100, "device": 50, "structure": 10}
CALLS = deque()
LIMIT = 20


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _gate(self):
        now = time.time()
        while CALLS and now - CALLS[0] > 60:
            CALLS.popleft()
        if not self.headers.get("Authorization", "").startswith("Bearer nle_"):
            self._send(401, {"error": "Unauthorized"})
            return False
        if len(CALLS) >= LIMIT:
            self._send(429, {"error": "Rate limit exceeded"})
            return False
        CALLS.append(now)
        return True

    def log_message(self, fmt, *args):
        print(f"[{time.strftime('%H:%M:%S')}] {self.command} {self.path} "
              f"({len(CALLS)}/{LIMIT} in last min)")

    def _ok(self, bucket):
        REV[bucket] += 1
        self._send(200, {"success": True, "message": "Command handled", "device": SERIAL,
                         "object": f"{bucket}.{SERIAL}", "revision": REV[bucket],
                         "timestamp": int(time.time() * 1000)})

    def do_GET(self):
        if not self._gate():
            return
        if self.path == "/api/v1/devices":
            return self._send(200, {"devices": [{"id": DEV_ID, "serial": SERIAL,
                                                 "name": STATE["shared"]["name"],
                                                 "accessType": "owner"}]})
        if self.path == f"/api/v1/thermostat/{DEV_ID}/status":
            ts = int(time.time() * 1000)
            return self._send(200, {
                "device": {"id": DEV_ID, "serial": SERIAL, "name": STATE["shared"]["name"]},
                "state": {
                    f"shared.{SERIAL}": {"object_revision": REV["shared"], "object_timestamp": ts,
                                         "value": STATE["shared"]},
                    f"device.{SERIAL}": {"object_revision": REV["device"], "object_timestamp": ts,
                                         "value": STATE["device"]},
                    "structure.mock-structure": {"object_revision": REV["structure"],
                                                 "object_timestamp": ts,
                                                 "value": STATE["structure"]},
                }})
        self._send(404, {"error": "Not found"})

    def do_POST(self):
        if not self._gate():
            return
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        print(f"    body: {body}")
        base = f"/api/v1/thermostat/{DEV_ID}"
        if self.path == base + "/temperature":
            if not isinstance(body.get("value"), (int, float)):
                return self._send(400, {"error": "Temperature value must be a number"})
            STATE["shared"]["target_temperature"] = float(body["value"])
            return self._ok("shared")
        if self.path == base + "/mode":
            if body.get("mode") not in ("heat", "cool", "heat-cool", "off"):
                return self._send(400, {"error": "Invalid mode"})
            STATE["shared"]["target_temperature_type"] = \
                "range" if body["mode"] == "heat-cool" else body["mode"]
            return self._ok("shared")
        if self.path == base + "/away":
            STATE["structure"]["away"] = bool(body.get("away"))
            STATE["device"]["eco"] = {"mode": "manual-eco" if body.get("away") else "schedule"}
            return self._ok("structure")
        if self.path in (base + "/fan", base + "/temperature/range", base + "/lock"):
            return self._ok("device")
        self._send(404, {"error": "Not found"})


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=9600)
    a = p.parse_args()
    print(f"Mock nolongerevil.com API on http://0.0.0.0:{a.port}/api/v1 (serial {SERIAL})")
    ThreadingHTTPServer(("0.0.0.0", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
