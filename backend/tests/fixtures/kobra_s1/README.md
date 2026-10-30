Offline fixture provenance (2026-10-29):

- `filament_hub.json`: release brief's scratchpad S1 Moonraker capture. Contains only object status, no host, device identifier or credential.
- `print_start_report.json`: first print/start report from the named print-test telemetry capture. Only progress/time/layer fields retained; message identity replaced.
- `idle_info_report.json`: first info/report from phase-a/idle-600s. Only state/speed fields retained; message identity replaced.
- `ace_status_sequence.json`: derived from kobra-s1-screen `docs/captures/swap-baseline.jsonl` by keeping only print state, `current_filament`, `auto_refill` and each slot's index, status, type and color at each change. Times are seconds from the capture's start.
- `reference_payload.py`: pure functions copied from kobra-s1-screen `odinscreen/files.py` at the brief's e8ed872 reference. Network and catalogue code excluded.

The telemetry capture's print/start messages are reports, not outbound requests. Payload equality tests therefore use the reference builder, with synthetic file identities and timestamps. No handshake captures or credentials are fixtures.
