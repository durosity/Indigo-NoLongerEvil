# Changelog

## 0.2.0
- Hot water / Heat Link states (experimental, from the NLE protocol docs): control detected,
  mode, boost active and end time, temperature, eco behaviour, raw fields reported
- Boost Hot Water action (experimental; self-hosted and embedded)
- Heat Link / hot water capture: records raw buckets and a change timeline while you use the
  hot water controls, with markers, then saves a redacted report and opens a pre-filled
  GitHub issue
- Self-hosted connections can read the server's database file for raw buckets
  (hot water states, captures, diagnostics)
- The plugin asks for a capture when hot water control, or an undocumented hot water field,
  is first seen
- `time_to_target` now treated as seconds remaining (per NLE docs); epoch values still work
- Simulator: `--heat-link` mode. Mock cloud API: hot water fields
- GitHub issue templates (Heat Link capture, bug report)

## 0.1.0
- First prototype: self-hosted, embedded and nolongerevil.com connections; native Indigo
  thermostat devices; eco, eco temperatures, range, fan timer; diagnostics; simulator and
  mock cloud API
