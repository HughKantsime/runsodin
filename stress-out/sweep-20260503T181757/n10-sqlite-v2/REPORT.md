# Stress run — `n10-sqlite-v2`

## Configuration

- Printers requested: **10** (seeded 10)
- Publisher rate: 10.0s per printer
- Run duration: 300.0s
- Broker: 127.0.0.1:1883
- Fixture: tests/fixtures/telemetry/bambu-x1c-ams-swap.jsonl

## Publisher

- Total publishes: 299
- Failures: 1
- Connect attempts: 10 (successful: 10)

## Backend `_on_status` / `_on_message` latency

| function | count | failures | p50 ms | p95 ms | p99 ms | max ms | mean ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| `BambuTelemetryAdapter._on_message` | 300 | 0 | 9.26 | 13.59 | 17.38 | 134.9 | 7.62 |
| `PrinterMonitor._on_status` | 300 | 0 | 7.99 | 12.13 | 16.03 | 132.71 | 6.43 |

## DB row growth

| table | start rows | end rows | delta |
|---|---:|---:|---:|
| `printer_telemetry` | 6 | 50 | +44 |
| `ams_telemetry` | 0 | 0 | +0 |
| `alerts` | 0 | 0 | +0 |

Samples: 20 (every ~30s)
