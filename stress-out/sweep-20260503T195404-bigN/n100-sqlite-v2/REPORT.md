# Stress run — `n100-sqlite-v2`

## Configuration

- Printers requested: **100** (seeded 100)
- Publisher rate: 10.0s per printer
- Run duration: 300.0s
- Broker: 127.0.0.1:1883
- Fixture: tests/fixtures/telemetry/bambu-x1c-ams-swap.jsonl

## Publisher

- Total publishes: 3003
- Failures: 1
- Connect attempts: 100 (successful: 100)

## Backend `_on_status` / `_on_message` latency

| function | count | failures | p50 ms | p95 ms | p99 ms | max ms | mean ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| `BambuTelemetryAdapter._on_message` | 3004 | 0 | 9.83 | 14.22 | 19.25 | 136.23 | 8.56 |
| `PrinterMonitor._on_status` | 3004 | 0 | 8.41 | 12.75 | 17.89 | 134.63 | 7.23 |

## DB row growth

| table | start rows | end rows | delta |
|---|---:|---:|---:|
| `printer_telemetry` | 78 | 499 | +421 |
| `ams_telemetry` | 0 | 0 | +0 |
| `alerts` | 0 | 0 | +0 |

Samples: 20 (every ~30s)
