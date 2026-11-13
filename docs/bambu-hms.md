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
A fatal/serious HMS fault can fail a matching active print only with fresh,
non-retained `FAILED`, `PAUSE` or `IDLE` corroboration from the same or a following
report. Retained HMS is display-only for job outcomes; common/info notices never
fail a job. There is no hand-maintained stopping-code list.

## Printer-camera detections

One deliberate exception to "no hand-maintained code list": the reviewed table
`PRINTER_DETECTION_CODES` in `backend/modules/vision/printer_native.py`. Printers
with built-in AI monitoring report what their own camera (or first-layer scanner)
saw as common notices or `print_error` values, which never alert under the rules
above. The vendor lists carry no machine-readable class for them, so the table maps
each such code to a kind: `spaghetti`, `first_layer`, `nozzle_clump`, `air_print`,
`shift_collapse`, `chute_pileup`. Camera-health, build-plate-marker and
clumping-sensor codes are listed beside it as explicitly not detections. Contract
tests check every table code against the bundled lists by keyword and fail when a
refreshed list adds a sentence on these subjects that has not been classified, so
review the table with each release refresh.

When a table code becomes active (an HMS item appears, or `print_error` takes the
value) in a live, non-retained report, O.D.I.N. records one detection
(`vision_detections`, confidence 1.0, metadata `{"source": "printer", "code",
"text"}`, linked to the running print when there is one) and raises the alert a
Vigil detection raises, to the same recipients. The persistent active set lives in
`system_config` (`printer.native_detection_codes.<printer id>`): a reconnect or
restart does not repeat a detection, a clear followed by recurrence is new, and a
second code of a kind that is already active is the same detection. Omitted or null
`hms` / `print_error` fields change nothing; an empty array or `0` clears.

This path only records and alerts. It never pauses, stops or fails a print (the
printer applies its own setting) and the job-outcome rules above are unchanged.
Vigil's auto-pause never claims a printer-source detection. Education prints are
recorded but not alerted, as for HMS alerts. If the printer has a camera in
O.D.I.N., one snapshot is attached off the monitor thread (3 second limit) and the
detection is `pending`; otherwise it is `informational` and never waits in the
review queue. No training frame is written. Alert types reuse Vigil's three
(`spaghetti_detected` for spaghetti, clumping, air printing and the waste chute,
the last as a warning; `first_layer_issue`; `detachment_detected` for shift or
collapse), so existing alert preferences apply.

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
