# Genuine P2S single-source regression fixture

`p2s-pla-source-sanitized.3mf` is a small derivative of production print file 49,
exported by Bambu Studio 02.08.02.61 with Bambu's P2S profile. The original is
private evidence and is not included in the repository.

- Source SHA-256: `0d2d7ddd3c633e7bd494945ce7265ce6cff83f8ea73af23f9362d2ad59f78c7a`.
- Fixture SHA-256: `8794aa79d2b9531b10fad0ae760fc39e89fa9bdaeac08b1b0b622b8f09ce32ba`;
  1308 bytes, with 18 retained G-code lines.
- Printer: Bambu Lab P2S, 0.4 mm Standard nozzle. Single used material: PLA,
  file id 1 (zero-based source index 0). `filament_map` is `["1"]`,
  `filament_nozzle_map` is `["0", "0"]`, slice `filament_maps` is `1`.
- Retained metadata filenames: `Metadata/slice_info.config`,
  `Metadata/project_settings.config`, and `Metadata/plate_1.json`.
  Values for source IDs, material, nozzle configuration, extruder maps and
  overall bounds were selected directly from the original metadata.
- `Metadata/plate_1.gcode` retains the original material and printer model
  headers, the end-template marker and every T/M620/M621 command in original
  order, including repetitions. Inline comments, geometry moves and all
  unrelated commands were removed. The fixture is for inspection tests and must
  not be printed.
- Removed meshes, thumbnails, object names and per-object bounds, unrelated
  profiles/settings, timing/weight metadata except retained filament facts, and
  all other archive members. ZIP timestamps are fixed.

Production 2.0.37 refused this file with "Unverifiable source mapping". Only
`M620.11 P0 L0 I0 E0`, `M620.11 K0 I0 R0` and `M620.6 I0 W1` caused that:
without them the unchanged function accepts it. This fixture validates parsing
and simulated dispatch, not a physical print.
