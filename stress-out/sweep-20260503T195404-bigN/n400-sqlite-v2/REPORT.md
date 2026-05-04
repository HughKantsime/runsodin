# Stress run — `n400-sqlite-v2`

## Configuration

- Printers requested: **400** (seeded 400)
- Publisher rate: 10.0s per printer
- Run duration: 300.0s
- Broker: 127.0.0.1:1883
- Fixture: tests/fixtures/telemetry/bambu-x1c-ams-swap.jsonl

## Publisher

- Total publishes: 12015
- Failures: 1
- Connect attempts: 340 (successful: 340)

## Backend `_on_status` / `_on_message` latency

| function | count | failures | p50 ms | p95 ms | p99 ms | max ms | mean ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| `BambuTelemetryAdapter._on_message` | 10186 | 0 | 7.56 | 13.63 | 17.35 | 255.56 | 6.94 |
| `PrinterMonitor._on_status` | 10186 | 0 | 6.75 | 12.51 | 16.21 | 253.56 | 6.06 |

## DB row growth

| table | start rows | end rows | delta |
|---|---:|---:|---:|
| `printer_telemetry` | 250 | 1695 | +1445 |
| `ams_telemetry` | 0 | 14 | +14 |
| `alerts` | 0 | 0 | +0 |

Samples: 30 (every ~30s)
