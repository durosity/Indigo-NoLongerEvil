# NoLongerEvil Nest for Indigo

Control Nest Learning Thermostats (Gen 1 and Gen 2) that have been flashed with
[NoLongerEvil](https://github.com/codykociemba/NoLongerEvil-Thermostat) firmware, from
[Indigo](https://www.indigodomo.com).

**Status: v0.2.0 prototype.** Built and tested against the real NoLongerEvil server code
with a simulated thermostat. Not yet tested with a physical thermostat.

> **Got a UK/EU Nest with a Heat Link?** Please help finish hot water support. Run
> **Plugins → NoLongerEvil Nest → Start Heat Link / Hot Water Capture** and submit the
> report as a [GitHub issue](https://github.com/durosity/Indigo-NoLongerEvil/issues/new?template=heat-link-capture.yml).
> See [Heat Link captures](#heat-link--hot-water-captures).

## Three ways to connect

Each connection is an **NLE Connection** device. You can mix them.

| Type | What it talks to | Updates |
| - | - | - |
| Self-hosted | The NLE Docker image or HA add-on (control port, normally 8082) | Instant (server event stream) plus a safety poll |
| Embedded | The NLE server running *inside this plugin*, no Docker needed | Instant (event stream) plus a safety poll |
| nolongerevil.com | The cloud API, using an `nle_` API key | Polled (the API allows 20 requests/min) |

Each thermostat is a native Indigo **thermostat** device, so it works with Indigo's
thermostat controls, control pages and HomeKit bridges. It provides the following:

- **Native states:** temperature, humidity, heat setpoint, cool setpoint (only if the
  system can cool), HVAC mode, and heater/cooler/fan running.
- **Extra states:** eco active, eco temperatures, Nest mode, online and last seen,
  time to target, leaf, learning mode, backplate temperature, firmware, IP and MAC.
- **Hot water (UK/EU Heat Link, experimental):** hot water control detected, mode, boost
  active, boost end time, hot water temperature, eco behaviour, and the list of raw hot
  water fields the thermostat reports.
- **Actions:** set eco on/off/toggle, set eco temperatures, set heat-cool range, run
  fan timer, boost hot water (experimental), and refresh.
- **Menu:** list thermostats on all connections, log diagnostics (full status plus raw Nest
  buckets where the connection can see them), Heat Link capture, refresh all, and toggle debug.

Capabilities (heat, cool, fan) are read from the thermostat, so a UK heat-only setup won't
show cooling or fan controls.

## Setup

### Embedded (no Docker)
1. Add an **NLE Connection** device and choose *Embedded server*. The defaults are thermostat
   port 9543 and control port 9544. The LAN IP is auto-detected.
2. The Event Log shows the address to give the thermostat, e.g. `http://192.168.1.50:9543`.
3. Flash the thermostat with the NoLongerEvil installer. Choose *Self-Hosted → NLE Server* and
   enter that address. Reboot the thermostat afterwards so it sends its full state.
4. Add a **Nest Thermostat** device, pick the connection, and pick the thermostat.

The first start installs the server's Python packages from `requirements.txt`, which
takes a minute or two. Port and IP changes need a plugin restart. Only one embedded
connection is allowed.

### Self-hosted Docker (e.g. OrbStack)
```bash
docker run -d --name nolongerevil-server --restart unless-stopped \
  -p 8000:8000 -p 8082:8082 \
  -e API_ORIGIN=http://YOUR_MAC_LAN_IP:8000 \
  -v nolongerevil-data:/data \
  ghcr.io/codykociemba/nolongerevil-selfhosted:latest
```
Add an **NLE Connection** device, choose *Self-hosted*, and set the URL to
`http://YOUR_MAC_LAN_IP:8082`. Use the control port, not 8000.

**Hot water states and captures need the server's database file.** The Docker control API
doesn't expose raw Nest buckets, so the plugin reads the container's SQLite database
(read-only) instead. Bind-mount the container's data folder to a folder on the Mac:

```bash
# where does this image keep its database?
docker exec nolongerevil-server printenv SQLITE3_DB_PATH   # e.g. /app/data/database.sqlite
```
Recreate the container with that folder mounted, e.g. `-v ~/nolongerevil:/app/data`.
Then set **Server database file** on the connection device to
`~/nolongerevil/database.sqlite`.

Check this even if you don't care about the plugin. NLE's docs mount `/data`, but the
server's own Dockerfile keeps the database in `/app/data`. If yours does too, the database
isn't on the volume and is lost whenever the container is recreated.

### nolongerevil.com
Create an API key at nolongerevil.com/settings → API Keys with **read** and **write** scopes.
Add an **NLE Connection** device, choose *nolongerevil.com*, and paste the key. The plugin
stays under the rate limit automatically. With several thermostats it stretches the poll
interval and keeps headroom for commands. Eco temperatures can't be set over the cloud API.

## Testing without a thermostat

`tools/` has two test helpers. Both run on the Mac with plain `python3`; the simulator
needs `requests`.

**`nest_simulator.py`** is a fake Gen 2 thermostat. It speaks the real device protocol, so
it works against the embedded server and the Docker server alike. It heats a pretend room
and obeys setpoint, mode and eco changes:
```bash
# embedded connection
python3 tools/nest_simulator.py --server http://127.0.0.1:9543
# Docker connection
python3 tools/nest_simulator.py --server http://127.0.0.1:8000
# a second thermostat
python3 tools/nest_simulator.py --server http://127.0.0.1:9543 --serial 02AA01AB01234599 --name Landing
```
Add `--heat-link` to include the documented hot water fields. It also adds one made-up
"undocumented" field and obeys hot water boosts, so you can try the hot water states, the
boost action and a full capture.

Start it, then add a Nest Thermostat device. "Hallway (simulated)" appears in the list.
Changing the setpoint in Indigo prints `<- server set target_temperature = …` in the
simulator, and its temperature changes appear in Indigo within a second or so.

**`mock_hosted_api.py`** is a stand-in for nolongerevil.com, built from the published API
docs. It enforces the 20/min limit too:
```bash
python3 tools/mock_hosted_api.py          # http://0.0.0.0:9600/api/v1
```
Create a *nolongerevil.com* connection with any key starting `nle_` and set **API base
URL** to `http://127.0.0.1:9600/api/v1`.

## Heat Link / hot water captures

Nobody has published exactly what a UK/EU Nest and Heat Link send to the NLE server. The
hot water states in this plugin come from NLE's protocol docs, and the docs and the server
code don't fully agree. The capture feature records the real thing:

1. **Plugins → NoLongerEvil Nest → Start Heat Link / Hot Water Capture…** Pick the
   thermostat, a duration (20 minutes is plenty) and describe your setup (boiler type,
   tank or not).
2. On the thermostat, work through the hot water controls: boost on, boost off, schedule,
   off, back to schedule. If you can, also toggle eco.
3. After each step use **Add Capture Marker…** to say what you just did.
4. **Finish Capture and Prepare GitHub Issue.** The plugin saves a redacted report, shows
   it in Finder, and opens a pre-filled GitHub issue. Drag the `.json` file into it and
   submit.

The report holds every raw bucket the server has, at the start and the end, plus a
timeline of every field that changed, including buckets from other serials, in case the
Heat Link reports separately. Serials become `SERIAL-1` and so on. IP and MAC addresses,
postcodes, e-mail, account and user details, pairing data and PIN hashes are blanked.
Have a quick look before submitting anyway.

Captures need a connection that can see raw buckets. That means embedded, nolongerevil.com
(30-second samples because of the rate limit), or self-hosted with the database file set.

The plugin also asks for a capture in the Event Log, once, when it first sees hot water
control, and whenever a thermostat reports a hot water field nobody has documented yet.

## When you get a real thermostat
**Log Thermostat Diagnostics…** writes the full status and raw buckets to the Event Log.
That's useful for bug reports and for the temperature-lock PIN format (see ROADMAP.md).
The device's own API key is redacted.

## Credits
- [NoLongerEvil](https://github.com/codykociemba/NoLongerEvil-Thermostat) by Cody Kociemba /
  Hack House. The embedded mode bundles its MIT-licensed self-hosted server (v1.0.1, commit
  `bca8781`) unmodified, under `Contents/Server Plugin/nolongerevil/`.
- MIT licence. See LICENSE.
