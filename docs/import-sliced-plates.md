# Import sliced 3MF plates as separate G-code files

`POST /print-files/import-plates` imports a sliced 3MF into separate models and
print files. It does not queue jobs or send printer commands. The existing
`POST /print-files/upload` response and Bambu plate selection remain unchanged.
This backend endpoint has no new UI.

Send multipart fields `file` and `target_printer_id`. The target is required for
every import, including packages with no recorded machine. The caller needs
`upload.upload` permission and the `write:models` token scope. The endpoint is
limited to 10 requests per minute. The target must be active, be visible under the existing upload policy
including shared printers, and use Moonraker or PrusaLink. Bambu targets and packages
with Bambu machine facts are refused. Use their original sliced 3MF workflow.

Each sparse archive plate number is preserved in `source_plate`. For example,
plates 1, 4 and 10 become `project_plate_000000001.gcode`,
`project_plate_000000004.gcode` and `project_plate_000000010.gcode`. Every file
gets its own model and immutable `print_file_target:<id>` binding. Padded names
prevent plate 1 from matching plate 10 through Moonraker filename substrings.
The response contains `files`, `plate_count` and `target_printer_id`; every file
entry contains its ID, model ID, filename, source plate and metadata.

Machine, bed and nozzle facts come from the parent package, with plate-specific
slice facts applied only to their own plate. Duplicate metadata keys, conflicting machine or nozzle declarations, an oversized
bed or a known nozzle mismatch are refused. A recorded bed requires target bed
dimensions. An unrecorded target nozzle does not imply a mismatch. The target
identity uses `machine_type` when present, otherwise the printer model.
`compatible_api_types` is set to the explicit target API, never left empty to
permit every API. An explicit source machine must match the target machine. Known aliases use the
shared canonical identity; unrecognized declarations must match after conservative
normalization. A missing target identity cannot validate an explicit source.
Missing source facts stay missing; choosing a target does not create machine evidence.

Grams, print time and filament entries come from each plate's `slice_info` index.
Each model also receives its own recorded filament requirements and material type
for scheduling and costing. Missing colors use an empty string so model responses
remain valid without creating a color requirement. Material labels outside Odin's
supported model types
stay on the print file; the model type remains unset and existing cost defaults
apply. Missing values stay missing rather than inheriting plate 1. Every imported
plate must have its own `slice_info` entry; a missing entry
is refused because its filament count cannot be checked. Source slot IDs must be
from 1 through 16. This initial importer requires exactly one used source filament
per plate. Existing non-Bambu target
metadata does not establish multi-material capability, so the importer cannot
safely admit MMU or multi-tool plates yet. No claim about the printer's physical
tool count is inferred from that limitation.

Before staging, Odin requires 428 MiB free on the storage volume: the maximum
upload and extracted payloads plus a 128 MiB reserve. At import start, it removes
unlocked staging directories older than one day. Active imports and final files
are left alone. An inaccessible stale directory is logged and skipped. Before
extraction, the worker checks space again for the declared payloads plus the reserve.

The upload cap is 100 MiB, with at most 32 plates, 64 MiB of G-code per plate and
200 MiB total extracted G-code. Individual metadata members are limited to 4 MiB.
The existing ZIP bounds also apply: 512 members, 4 MiB central directory,
400 MiB per member and 500 MiB total expansion. Plate members use the canonical
`Metadata/plate_N.gcode` rules; traversal, symlinks, duplicate names, encrypted
members and ambiguous plate numbers are refused. Archive names never select a
filesystem destination.

The existing bounded 3MF child pool handles extraction. Staging and hashing
finish off the event loop before the upload lock is acquired. One short database
transaction creates the models, files and bindings, then generated staged files
move atomically on the same storage filesystem before commit. A confirmed
rollback removes every staged and final file. As with ordinary uploads, a lost
commit acknowledgement or failed rollback leaves the outcome uncertain. Odin
retains final payloads and includes every reserved file ID in the support response
and log instead of deleting files that may already have committed references.
A failed recovery move can leave that payload unavailable. The failure is logged
per file and does not suppress the file-ID report. Do not retry an uncertain
import until its outcome has been checked.
