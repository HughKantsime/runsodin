# Stress run — `n200-sqlite-v2`

## Configuration

- Printers requested: **200** (seeded 200)
- Publisher rate: 10.0s per printer
- Run duration: 300.0s
- Broker: 127.0.0.1:1883
- Fixture: tests/fixtures/telemetry/bambu-x1c-ams-swap.jsonl

## Publisher

- Total publishes: 6005
- Failures: 1
- Connect attempts: 200 (successful: 200)

## Backend `_on_status` / `_on_message` latency

| function | count | failures | p50 ms | p95 ms | p99 ms | max ms | mean ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| `BambuTelemetryAdapter._on_message` | 6006 | 0 | 9.52 | 13.6 | 15.25 | 133.67 | 7.93 |
| `PrinterMonitor._on_status` | 6006 | 0 | 8.18 | 12.36 | 14.01 | 130.85 | 6.73 |

## DB row growth

| table | start rows | end rows | delta |
|---|---:|---:|---:|
| `printer_telemetry` | 174 | 1000 | +826 |
| `ams_telemetry` | 0 | 10 | +10 |
| `alerts` | 0 | 0 | +0 |

Samples: 22 (every ~30s)
