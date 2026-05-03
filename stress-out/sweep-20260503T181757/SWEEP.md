# MQTT stress sweep — 2026-05-03T22:33:50Z

Matrix: N ∈ {10, 25, 50} × sqlite × V2=1; 5 min/cell, 10s heartbeat throttle


---
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

---
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

---
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
