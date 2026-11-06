"""ODIN printer telemetry V2 — typed, fail-loud ingestion pipeline.

Gated by env var ODIN_TELEMETRY_V2. When 0 (default), legacy adapters in
`backend/modules/printers/adapters/` handle ingestion. When 1, this package
takes over for Bambu only; Moonraker/Prusa/Elegoo continue on legacy.

Shipped as part of track `telemetry-rewrite-bambu-first_20260417`.
Empirical grounding: odin-e2e/captures/run-2026-04-16.
"""
