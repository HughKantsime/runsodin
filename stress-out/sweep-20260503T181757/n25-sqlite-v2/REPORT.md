# Stress run — `n25-sqlite-v2`

## Configuration

- Printers requested: **25** (seeded 25)
- Publisher rate: 10.0s per printer
- Run duration: 300.0s
- Broker: 127.0.0.1:1883
- Fixture: tests/fixtures/telemetry/bambu-x1c-ams-swap.jsonl

## Publisher

- Total publishes: 751
- Failures: 1
- Connect attempts: 25 (successful: 25)

## Backend `_on_status` / `_on_message` latency

| function | count | failures | p50 ms | p95 ms | p99 ms | max ms | mean ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| `BambuTelemetryAdapter._on_message` | 752 | 0 | 9.44 | 13.82 | 18.67 | 113.21 | 8.02 |
| `PrinterMonitor._on_status` | 752 | 0 | 8.21 | 12.4 | 17.23 | 111.15 | 6.89 |

## DB row growth

| table | start rows | end rows | delta |
|---|---:|---:|---:|
| `printer_telemetry` | 18 | 124 | +106 |
| `ams_telemetry` | 0 | 0 | +0 |
| `alerts` | 0 | 0 | +0 |

Samples: 20 (every ~30s)
