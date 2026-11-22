"""Heat Link / hot water data capture.

Nobody has published what a UK Gen 2 + Heat Link actually sends to the NLE
server, so this records it: a baseline of every raw bucket the server holds,
then a timeline of every field that changes while the user works the hot
water controls on the thermostat, plus their own markers ("pressed boost
1 hour"). The result is redacted and written as a JSON report and a Markdown
summary, and the user is pointed at a pre-filled GitHub issue to submit it.
"""

import json
import os
import re
import time
import urllib.parse
from datetime import datetime

ISSUE_URL = "https://github.com/durosity/Indigo-NoLongerEvil/issues/new"
ISSUE_TEMPLATE = "heat-link-capture.yml"

# Fields whose values identify a person, home or network
REDACT_KEYS = re.compile(
    r"(mac_address|local_ip|ip_address|ipv4|ipv6|postal_code|zip_code|email|latitude|"
    r"longitude|ssid|wifi|password|passphrase|api_key|token|secret|pin_hash|user_id|"
    r"serial_number|street|address|city|phone|weave|pairing|entry_key|session)",
    re.I,
)
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
MAC = re.compile(r"\b[0-9a-f]{2}(?:[:-]?[0-9a-f]{2}){5}\b", re.I)
# Changes to these are expected background noise; kept, but flagged
NOISY = re.compile(
    r"(current_temperature|backplate_temperature|current_humidity|battery_level|"
    r"time_to_target|last_connection|object_timestamp|object_revision|_timestamp$|"
    r"leaf_|safety_state_time|uptime|rssi|wifi_|heat_link_rssi)",
)


def _flatten(prefix, value, out):
    if isinstance(value, dict):
        for k, v in value.items():
            _flatten(f"{prefix}.{k}" if prefix else str(k), v, out)
    else:
        out[prefix] = value
    return out


def flatten_snapshot(snapshot):
    """{serial: {object_key: {object_revision, value}}} -> {path: value}."""
    flat = {}
    for serial, buckets in (snapshot or {}).items():
        for key, obj in (buckets or {}).items():
            value = obj.get("value", obj) if isinstance(obj, dict) else obj
            _flatten(f"{serial}|{key}", value, flat)
    return flat


class Redactor:
    """Consistent pseudonyms for serials and ids, blanking of personal fields."""

    def __init__(self):
        self.map = {}

    def alias(self, real, kind):
        if real not in self.map:
            n = sum(1 for v in self.map.values() if v.startswith(kind)) + 1
            self.map[real] = f"{kind}-{n}"
        return self.map[real]

    def learn(self, snapshot):
        for serial, buckets in (snapshot or {}).items():
            self.alias(serial, "SERIAL")
            for key in buckets or {}:
                bucket, _, ident = key.partition(".")
                if ident and ident != serial and bucket in ("structure", "user", "where",
                                                             "link", "rcs_settings"):
                    self.alias(ident, bucket.upper())

    def text(self, value):
        if not isinstance(value, str):
            return value
        for real, alias in self.map.items():
            if real and real in value:
                value = value.replace(real, alias)
        value = IPV4.sub("<ip>", value)
        return MAC.sub("<mac>", value)

    def value(self, key, value):
        if REDACT_KEYS.search(str(key)):
            return "<redacted>" if value not in (None, "", False, 0) else value
        if isinstance(value, dict):
            return {self.text(k): self.value(k, v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.value(key, v) for v in value]
        return self.text(value)

    def bucket(self, key, obj):
        # The user bucket is about a person (name, email...) - keep only its shape
        if str(key).startswith("user.") and isinstance(obj, dict):
            value = obj.get("value", {})
            return {**obj, "value": {k: "<redacted>" for k in (value or {})}}
        return self.value(key, obj)

    def snapshot(self, snapshot):
        return {self.text(serial): {self.text(k): self.bucket(k, v) for k, v in buckets.items()}
                for serial, buckets in (snapshot or {}).items()}


class Capture:
    def __init__(self, dev, conn_kind, serial, minutes, note, first_snapshot, plugin_version):
        self.dev_id = dev.id
        self.dev_name = dev.name
        self.conn_kind = conn_kind
        self.serial = serial
        self.started = time.time()
        self.ends = self.started + minutes * 60
        self.note = note
        self.plugin_version = plugin_version
        self.baseline = first_snapshot
        self.last_flat = flatten_snapshot(first_snapshot)
        self.last_snapshot = first_snapshot
        self.events = []  # {"t", "type": "change"|"marker", ...}
        self.samples = 1
        self.next_sample = 0.0

    @property
    def remaining(self):
        return max(0, int(self.ends - time.time()))

    def add_marker(self, text):
        self.events.append({"t": time.time(), "type": "marker", "text": text})

    def add_snapshot(self, snapshot):
        """Diff against the previous sample; returns the number of changed paths."""
        self.samples += 1
        flat = flatten_snapshot(snapshot)
        changes = 0
        for path in sorted(set(flat) | set(self.last_flat)):
            old, new = self.last_flat.get(path, "<absent>"), flat.get(path, "<absent>")
            if old != new:
                changes += 1
                self.events.append({"t": time.time(), "type": "change", "path": path,
                                    "old": old, "new": new,
                                    "noise": bool(NOISY.search(path))})
        self.last_flat = flat
        self.last_snapshot = snapshot
        return changes

    # ----------------------------------------------------------- report
    def _hot_water_paths(self, flat):
        return {p: v for p, v in flat.items()
                if re.search(r"hot_water|heat_link|heatlink|boiler|opentherm|kryptonite|topaz",
                             p, re.I)}

    def build_report(self, firmware="", model=""):
        red = Redactor()
        red.learn(self.baseline)
        red.learn(self.last_snapshot)
        base_flat = flatten_snapshot(self.baseline)
        final_flat = flatten_snapshot(self.last_snapshot)
        hw_paths = self._hot_water_paths({**base_flat, **final_flat})
        buckets = sorted({p.split("|", 1)[1].split(".", 1)[0]
                          for p in {**base_flat, **final_flat}})
        other_serials = sorted(s for s in {**self.baseline, **self.last_snapshot}
                               if s != self.serial)

        def path(p):
            serial, _, rest = p.partition("|")
            return f"{red.text(serial)}|{red.text(rest)}"

        timeline = []
        for e in self.events:
            item = {"seconds": round(e["t"] - self.started, 1), "type": e["type"]}
            if e["type"] == "marker":
                item["text"] = red.text(e["text"])
            else:
                leaf = e["path"].rsplit(".", 1)[-1]
                if "|user." in e["path"]:
                    leaf = "user_id"  # anything in the user bucket is personal
                item.update(path=path(e["path"]), old=red.value(leaf, e["old"]),
                            new=red.value(leaf, e["new"]), noise=e["noise"])
            timeline.append(item)

        return {
            "report": "Indigo-NoLongerEvil Heat Link / hot water capture",
            "format": 1,
            "plugin_version": self.plugin_version,
            "connection_type": self.conn_kind,
            "captured_at": datetime.fromtimestamp(self.started).strftime("%Y-%m-%d %H:%M"),
            "duration_seconds": int(time.time() - self.started),
            "samples": self.samples,
            "firmware": firmware,
            "model_version": model,
            "thermostat": red.text(self.serial),
            "other_serials_on_server": [red.text(s) for s in other_serials],
            "bucket_types": buckets,
            "user_note": red.text(self.note),
            "hot_water_fields_at_end": {path(p): red.value(p.rsplit(".", 1)[-1], v)
                                        for p, v in sorted(hw_paths.items())},
            "timeline": timeline,
            "baseline": red.snapshot(self.baseline),
            "final": red.snapshot(self.last_snapshot),
        }

    @staticmethod
    def summary_markdown(report):
        hw = report["hot_water_fields_at_end"]
        changes = [e for e in report["timeline"] if e["type"] == "change" and not e["noise"]]
        markers = [e for e in report["timeline"] if e["type"] == "marker"]
        lines = [
            f"- Plugin {report['plugin_version']}, {report['connection_type']} connection",
            f"- Firmware: {report['firmware'] or 'unknown'}"
            + (f", model {report['model_version']}" if report["model_version"] else ""),
            f"- {report['samples']} samples over {report['duration_seconds'] // 60} min, "
            f"{len(changes)} non-noise changes, {len(markers)} markers",
            f"- Bucket types: {', '.join(report['bucket_types'])}",
            f"- Other serials on server: {len(report['other_serials_on_server'])}",
            "",
            "**Hot water / Heat Link fields:**",
        ]
        lines += [f"- `{p}` = `{json.dumps(v)}`" for p, v in hw.items()] or ["- none found"]
        if markers:
            lines += ["", "**Markers:**"] + [f"- {m['seconds']}s: {m['text']}" for m in markers]
        return "\n".join(lines)

    def write(self, folder, firmware="", model=""):
        os.makedirs(folder, exist_ok=True)
        report = self.build_report(firmware, model)
        stamp = datetime.fromtimestamp(self.started).strftime("%Y%m%d-%H%M%S")
        base = os.path.join(folder, f"heatlink-capture-{stamp}")
        with open(base + ".json", "w") as fh:
            json.dump(report, fh, indent=2, sort_keys=False, default=str)
        summary = self.summary_markdown(report)
        with open(base + ".md", "w") as fh:
            fh.write("# Heat Link / hot water capture\n\n" + summary + "\n")
        return base + ".json", summary, report

    @staticmethod
    def issue_url(report, summary):
        params = {
            "template": ISSUE_TEMPLATE,
            "title": f"[Heat Link capture] firmware {report['firmware'] or 'unknown'}",
            "plugin_version": report["plugin_version"],
            "connection": report["connection_type"],
            "summary": summary[:3000],
        }
        return ISSUE_URL + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
