# Genuine H2D single-source regression fixture

`h2d-petg-source-sanitized.3mf` is a small derivative of Shane's print file 13,
`latch-v3.2-H2D-150-slot2-HF.gcode.3mf`, exported by Bambu Studio 02.08.02.61.
The original is private evidence and is not included in the repository.

- Source SHA-256: `fa7f45495157764e9f96cabcdce0c6111d31fd19a5cb33cd59eeb6dda933b509`.
- Fixture SHA-256: `97ffd50e53b3b0179afd4d157473d43ea712142e8b57d02ffde94c0e99b47022`;
  1,349 bytes, with 29 retained G-code lines.
- Printer: Bambu Lab H2D, 0.4 mm High Flow nozzles. Single used material: PETG,
  file id 2 (zero-based source index 1). Both saved profiles are PETG;
  `filament_map` is `["1", "1"]`, slice `filament_maps` is `1 1`.
- Retained metadata filenames: `Metadata/slice_info.config`,
  `Metadata/project_settings.config`, and `Metadata/plate_1.json`.
  Values for source IDs, material, nozzle configuration, extruder maps and
  overall bounds were selected directly from the original metadata.
- `Metadata/plate_1.gcode` retains the original material header and every
  T/M620/M621 command in original order, including repetitions. Inline comments,
  geometry moves and all unrelated commands were removed. The fixture is for
  inspection tests and must not be printed.
- Removed meshes, thumbnails, object names and per-object bounds, unrelated
  profiles/settings, timing/weight metadata except retained filament facts and
  first-layer timing, and all other archive members. ZIP timestamps are fixed.
- Privacy: retained fields are limited to the above manufacturing facts;
  decompressed members checked for serial/access-code markers, IP addresses and
  user paths. None are present. No credentials were requested or used.

Production 2.0.26 refused explicit `[-1, 1]` before upload. The supplied incident
record reports a successful direct MQTT start with only `ams_mapping [-1, 1]`.
This fixture validates parsing and simulated dispatch, not a new physical print.
