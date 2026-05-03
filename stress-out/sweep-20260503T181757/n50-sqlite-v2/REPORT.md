# Stress run — `n50-sqlite-v2`

## Configuration

- Printers requested: **50** (seeded 50)
- Publisher rate: 10.0s per printer
- Run duration: 300.0s
- Broker: 127.0.0.1:1883
- Fixture: tests/fixtures/telemetry/bambu-x1c-ams-swap.jsonl

## Publisher

- Total publishes: 1502
- Failures: 1
- Connect attempts: 50 (successful: 50)

## Backend `_on_status` / `_on_message` latency

| function | count | failures | p50 ms | p95 ms | p99 ms | max ms | mean ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| `BambuTelemetryAdapter._on_message` | 1503 | 0 | 10.01 | 14.37 | 18.78 | 112.64 | 8.67 |
| `PrinterMonitor._on_status` | 1503 | 0 | 8.58 | 12.91 | 17.36 | 110.43 | 7.35 |

## DB row growth

| table | start rows | end rows | delta |
|---|---:|---:|---:|
| `printer_telemetry` | 39 | 248 | +209 |
| `ams_telemetry` | 0 | 0 | +0 |
| `alerts` | 0 | 0 | +0 |

Samples: 20 (every ~30s)
