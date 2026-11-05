# Release data

Claude generates the real per-prefix JSON files and index with
`ops/fetch_bambu_hms.py` before the release. No synthetic test data belongs here.
See `docs/bambu-hms.md` and the root `NOTICE` for provenance and refresh steps.
A missing list produces plain codes; runtime never downloads replacements.
