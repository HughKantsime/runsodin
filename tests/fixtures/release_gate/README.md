# Candidate-gate 3MF fixture

`candidate-calibration-cube.3mf` is a deterministic, synthetic, unsliced 10 mm
cube used only by the release-blocking browser upload test. It contains no
customer model, thumbnail, printer serial, network address, or slicer profile.

Regenerate from this directory with:

```text
cd 3mf-src && zip -X -r ../candidate-calibration-cube.3mf '[Content_Types].xml' _rels 3D Metadata
```
