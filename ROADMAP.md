# Roadmap

## Needs a real thermostat first
- **Hot water / Heat Link (UK/EU) — waiting for captures.** v0.2.0 reads the documented
  fields (`has_hot_water_control`, `hot_water_mode`, `hot_water_boost_time_to_end`,
  `hot_water_away_enabled`, `hot_water_temperature`) plus `hot_water_active` from the
  server's write whitelist, and offers an experimental boost. Captures from real Heat Links
  are needed to confirm all of this: which fields exist, whether the Heat Link reports under
  its own serial, how boost and schedule behave, and whether hot water mode can be set.
- **Temperature lock.** Left out on purpose: the NLE docs' PIN-hash example doesn't match
  their stated algorithm (Base64(SHA1(PIN+serial))), and a wrong hash could lock the
  physical thermostat. Set a lock on the device, read `temp_lock_pin_hash` from the raw
  dump, confirm the scheme, then add the action.
- **Verify guesses** made from source/docs rather than real payloads:
  - `eco_mode` values (assumed `schedule` = normal, anything else = eco)
  - `time_to_target` units (docs say seconds remaining; both seconds and epoch are accepted)
  - hosted API status shape beyond the documented example
  - Heat Link capability flags (`can_cool` false, `has_fan` false expected)

## Later
- Read and edit the weekly schedule (`/api/schedule`, `set_schedule`)
- Optional auto-create of Indigo devices for newly seen thermostats
- Target humidity, safety temperatures and learning-mode toggles via `set_device_setting`
- Gen 3 support if NoLongerEvil adds it
