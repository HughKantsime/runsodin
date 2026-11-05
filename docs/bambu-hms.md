# Bambu health and print codes

O.D.I.N. reads Bambu Lab's per-model English lists bundled in each release.
There is no runtime fetch, scheduler, network fallback or user data sent to
Bambu. Missing, malformed or hash-mismatched data produces a plain code with
a Bambu troubleshooting link. Empty descriptions are intentionally hidden.

The model comes from the first three characters of the printer serial in the
encrypted `printers.api_key` (`serial|access_code`). Neither credential is
logged. Internal response enrichment carries only the three-character model family,
excluded from serialized API responses, and never retains a credential.

Severity follows Bambu Studio `DevHMSItem::parse_hms_info`: high 16 bits of
`code`, not `attr`; 1 fatal, 2 serious, 3 common, 4 info. Only fatal and serious
HMS events create printer alerts. Common notices, including possible spaghetti,
have no overrides. Active notices are shown once on the printer detail page;
last-seen codes are labelled as history. An explicit empty HMS array clears the
active set; omitted/null arrays do not. Persistent active code identities prevent
reconnects/restarts from generating the same alert, even after six hours or
reading/dismissing the prior alert. A clear followed by recurrence is new.
Job failure follows `gcode_state`, never a guessed HMS code list.

## Release refresh

Run from the repository root using Python 3 (standard library only):

```sh
python3 ops/fetch_bambu_hms.py
```

The default prefix set is the legacy families (00M, 00W, 03W, 01S, 01P,
030, 039) plus Bambu Studio's current packaged families (094, 239, 093,
20P, 22E, 31B, 26A). This is a superset of ODIN's supported Bambu models.
It deliberately fetches the entire upstream set without inferring a serial
family from a display model name. Add future families with `--prefix ABC`.
The spec verifies 00M, 01P, 039, 22E and 094 directly. The current Studio
family set was verified from the cached November 1 upstream `HMS.cpp`
research accompanying this spec; the older local Studio checkout contains
only 094, 239, 093 and 22E. Source:
https://github.com/bambulab/BambuStudio/blob/master/src/slic3r/GUI/HMS.cpp

The fetcher allows only the fixed HTTPS endpoint with validated three-character
prefixes, rejects redirects, uses a 20-second socket timeout, reads at most
8 MiB plus one sentinel byte per response, and validates both English tables.
It obtains and validates all responses before replacing any last-good files.
Each replacement is atomic; index is written last. Disk failures during the
replacement phase can leave mixed vendor versions: inspect the diff and rerun
before committing. There are no retries or alternate providers.

Each file includes the vendor version, UTC fetch timestamp, source URL and
SHA-256 over the canonical UTF-8 response JSON (sorted keys, no whitespace).
The index repeats this provenance. Docker's existing `COPY backend/` includes
the data. Review and commit all JSON files with the release, then restart the
application after replacing bundles; per-prefix data is cached in memory.

## Attribution and removal

The data is Bambu Lab's text; see `NOTICE`. Bambu Studio publishes corresponding
lists under AGPL-3.0 in `resources/hms`; O.D.I.N. keeps them as separate data,
not source constants. If Bambu objects, remove `backend/data/bambu_hms/*.json`
from the next release and retain this notice. Plain-code behavior needs no
code change. Existing alert rows are not deleted or rewritten by this feature.

Tests use short, authored synthetic response envelopes and paraphrased meanings
for the production examples in the spec. They are not fetched vendor lists.
The older V2 shadow telemetry catalog is not the active legacy monitor's decoder.
