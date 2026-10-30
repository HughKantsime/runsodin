# Dispatch capability and asynchronous starts

`POST /api/jobs/{id}/dispatch` and `POST /api/printers/{id}/start-next` return **202** when the durable preparing claim is committed, or when staging exceeds 30 seconds while its worker is still running. The timeout response carries a durable attempt handle even if claim acquisition is still pending. `accepted: true` means staging succeeded, not that the printer accepted START. The response contains `job_id`, `attempt_id` and `status_url`. Poll that URL with the same authenticated principal. Phases include `preparing`, `uploading`, `starting`, `start_accepted`, `refused` and `needs_reconciliation`. The existing unqualified status endpoint still supplies correlated layer observations.

A duplicate job POST receives 409. Start-next replays its existing actor-scoped request id and never starts another upload. Up to eight background workers can run per API process; target locks and database claims still exclude other processes and duplicate targets. The worker owns the target lock throughout preparation, transport and cleanup. Request sessions are not shared with workers. A restart does not resume workers or retry START: retained claims require the existing reconciliation workflow. Status records in `system_config` are read-only evidence, never dispatch authority. Attempt-qualified reads use the claim state for accepted/uncertain outcomes and report `needs_reconciliation` for an orphaned preparing/admitted worker. Active workers can display `uploading` and `starting`. Terminal workers delete their `dispatch_progress:<token>` row and keep only the latest terminal result per job; superseded handles return 404. Shutdown stops worker admission and drains existing workers for at most 30 seconds before the timeline writer stops.

Pre-claim validation refusals retain HTTP **400**; an existing claim returns **409** and worker capacity/start failures return **503**. Start-next restores its assignment and releases its suggestion guard if staging cannot start. A staging timeout is **202**, never a retry-authorizing failure.

Compatibility: append `?wait=true` to either POST for the previous synchronous result (HTTP 200 on success). The request stays open for the whole send, so a reverse proxy can answer 504 while the send continues. A proxy timeout is not a failure: read the job's dispatch status and never retry on it. Prefer the default 202 path. Internal scheduler callers retain synchronous `dispatch_job`. Web callers poll GET only, with no automatic POST retry; a bounded polling timeout leaves the attempt unconfirmed and fenced. No MCP tool definitions were found in this repository; external clients should adopt the handle or request the compatibility path.

## Nozzle gate

Bambu dispatch checks static model compatibility and geometry before claiming the printer, then compares fresh nozzle data from the exact staged archive before adapter admission or upload. Mismatches release the preparing claim. A post-upload recheck requires a new fresh, non-retained, idle printer report through the owned-process acquisition boundary; a confirmed gate refusal before START releases the admitted claim without marking the outcome uncertain. Transport exceptions and unconfirmed cleanup retain the reconciliation fence. It never uses cached installed-nozzle observations to authorize START. Positive diameter, flow or stainless-versus-required-hardness mismatches refuse with `nozzle_mismatch`; detail names the physical side, required value and reported value. Unknown requirements or observations allow dispatch with a `nozzle_unverified` job timeline event. Transport/readiness failure still refuses. Gate refusals reach the existing timeline, alert and dispatch-result paths. Timeline detail uses bounded structured fields; failed unverified-note enqueueing never blocks START. Nozzle acquisition runs before AMS acquisition, which receives the remaining shared ten-second budget, so the second read cannot silently age the first proof beyond its freshness window. Report mode waits for `device.nozzle.info` on dual printers or `nozzle_type` on single printers; top-level fields never stand in for dual-printer extruder 0.

`plate_N.json` selects used filaments with **zero-based** plate-level `filament_ids` (`[1]` is file filament id 2). These index `filament_nozzle_map` or `filament_map`. When plate ids are absent, `slice_info.config` selects used filaments with one-based ids. That map is one-based, then `physical_extruder_map` gives physical 0 (right) or 1 (left). The supplied `[1,1]` map and `[1,0]` physical map therefore select the left nozzle. File 10's Standard requirement refuses against High Flow, while file 11's High Flow requirement passes. Selected-plate assignments do not inherit another plate’s requirements.

The source hypothesis about the first character indicating material was incorrect. Bambu Studio's [DevNozzleSystem parser](https://github.com/bambulab/BambuStudio/blob/master/src/slic3r/GUI/DeviceCore/DevNozzleSystem.cpp) decodes flow from character two and material from suffix `00` (stainless), `01` (hardened steel), or `05` (tungsten carbide). Thus `HH05` reports High Flow / tungsten carbide, and says nothing about diameter. For dispatch, only flow letters S and H are treated as known. Legacy `hardened_steel`/`stainless_steel` strings establish material only and leave flow unknown, as do all other flow letters. Mapping evidence also comes from [Extruder::nozzle_id](https://github.com/bambulab/BambuStudio/blob/master/src/libslic3r/Extruder.cpp).

Printer responses and `/nozzle-status` expose `installed_nozzles` with physical index, side, diameter, flow, material, wear and observation time. The monitor preserves last-reported observations across packets without nozzle data. The printer detail page labels these as last-reported values, not fresh authorization.

## Geometry and migration

Total XY defaults in millimetres:

| Models | X × Y |
|---|---|
| X1, X1C, X1E, P1P, P1S, P2S, A1 | 256 × 256 |
| A1 Mini | 180 × 180 |
| H2D, H2D Pro | 350 × 320 |
| H2C | 330 × 320 |
| H2S | 340 × 320 |

Sources: Bambu Studio's [machine profiles](https://github.com/bambulab/BambuStudio/tree/master/resources/profiles/BBL/machine), the [H2D specification](https://cdn1.bambulab.com/documentation/h2d/en/H2D_Laser_Full_Combo_20250305.pdf), [H2S announcement](https://blog.bambulab.com/h2s-the-ultimate-single-nozzle-3d-printer-now-bigger-than-ever/), and [H2C specification](https://csm.bblcdn.com/hub/eca403f48aee405393afc97adbbf6422.pdf). Consulted 2026-10-27.

H2D reach comes from the owner's figures in `.claude/specs/dispatch-capability.md`: left X 0–325, right X 25–350. These agree with the current H2D and H2D Pro Studio `extruder_printable_area` profiles. The selected plate's `bbox_all` and `bbox_objects` are checked; object filament assignments narrow per-nozzle checks when available, a single used extruder checks `bbox_all` against that nozzle’s reach. With multiple extruders and no per-object filament assignments, only the bed bounds are enforced and an unverified geometry note is recorded. The supplied file 10/11 evidence has 150 objects (four sampled in the evidence JSON), no per-object filament assignments, and plate X 36.60945–288.39055, entirely within the left nozzle’s 0–325 reach. Both bed overflow and reach overflow use `bed_geometry_mismatch`. This does not simulate arbitrary G-code motion; missing bounds cannot establish geometric compatibility. Bambu's old whole-slicer-bed comparison is replaced by actual plate metadata bounds.

Migration 041 adds nullable observations and corrects only Bambu H2D/H2D Pro records whose stored pair is exactly 320×320, the shipped wrong UI default. All other pairs, including partial/custom values, are preserved. A custom value identical to the old default is indistinguishable without historical provenance. Fresh defaults cover every model currently listed in ODIN. No physical printer or production system was used to verify these changes.

The committed 2.0.23 release ledger remains unchanged. Migration 041 is recorded under **2.0.24**; `VERSION` and its mirrors remain for the release owner to bump. `preserved040` freezes the committed 040 schema on the existing immutable restore base, so 2.0.23 archives remain routable.

The web UI polls a 202 handle using GET only and displays the server’s `status.message` for both `refused` and `needs_reconciliation`, including nozzle mismatch details.

## Fix-loop verification

See [the verification report](dispatch-capability-verification.html) for each reviewed finding, regression coverage, and environment limitations. Focused backend and frontend checks pass; full release-precheck certification still requires local-network test support. No production access or commits were made.
