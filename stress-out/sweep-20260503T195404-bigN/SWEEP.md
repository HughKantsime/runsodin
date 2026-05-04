# MQTT stress sweep (big-N) — 2026-05-04T00:12:44Z

Matrix: N ∈ {100, 200, 400} × sqlite × V2=1; 5 min/cell, 10s heartbeat throttle


---
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

---
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

---
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
