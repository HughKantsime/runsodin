# Genuine X1 Carbon single-source regression fixture

`x1c-pla-source-sanitized.3mf` is byte-identical to the actual Bambu Studio export used for the October 19, 2026 X1C acceptance attempt. The name follows the existing telemetry fixture convention; no archive contents were rewritten or removed.

- SHA-256: `bbb779614d029d3c07a723770c1f1f02b82890183d0139bb55d2f50949935f07`
- Size: 45142 bytes.
- Slicer: Bambu Studio 02.08.02.61, actual CLI slicing/export.
- Geometry: owned 10 x 10 x 2 mm coupon reused from the A1 test; its retained object filename describes geometry only.
- Official presets: Bambu Lab X1 Carbon 0.4 nozzle, 0.20mm Standard @BBL X1C, Generic PLA, Textured PEI Plate. Inherited settings and included G-code templates were resolved from installed official presets without editing their G-code.
- Official X1 start-template SHA-256: `cc890a986c3f073e4691908d4bd3256222da9afc52fa9520261318bea9757505`.
- Single material: PLA, source index 0. Stock calibration includes exact standalone T1100; source changes still use M620/M621 S0A.
- Privacy: decompressed archive checked for the supplied printer access code, serial and address; none are present. Independent review found no credential markers or local user paths.

Published 2.0.1 rejects this fixture as unverifiable source mapping. The regression exercises the actual failure; the narrow exception must not admit other tools, command parameters, source conflicts or inconsistent material metadata. No physical print success is implied by this fixture.
