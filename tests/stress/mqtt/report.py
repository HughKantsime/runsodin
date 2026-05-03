"""Read latency.jsonl + samples.jsonl + bootstrap.json + publisher.json from
one stress run and emit a markdown summary.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    frac = k - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def summarize_latency(records: list[dict]) -> dict:
    by_fn: dict[str, list[float]] = defaultdict(list)
    failures_by_fn: dict[str, int] = defaultdict(int)
    for r in records:
        by_fn[r["fn"]].append(r["duration_ms"])
        if not r.get("ok", True):
            failures_by_fn[r["fn"]] += 1
    result = {}
    for fn, durs in by_fn.items():
        result[fn] = {
            "count": len(durs),
            "failures": failures_by_fn[fn],
            "p50_ms": round(percentile(durs, 50), 2),
            "p95_ms": round(percentile(durs, 95), 2),
            "p99_ms": round(percentile(durs, 99), 2),
            "max_ms": round(max(durs), 2),
            "mean_ms": round(statistics.mean(durs), 2),
        }
    return result


def render(run_dir: Path) -> str:
    latency = load_jsonl(run_dir / "latency.jsonl")
    samples = load_jsonl(run_dir / "samples.jsonl")
    bootstrap = load_json(run_dir / "bootstrap.json")
    publisher = load_json(run_dir / "publisher.json")

    parts: list[str] = []
    parts.append(f"# Stress run — `{run_dir.name}`\n")

    parts.append("## Configuration\n")
    parts.append(f"- Printers requested: **{bootstrap.get('requested', '?')}** "
                 f"(seeded {bootstrap.get('succeeded', '?')})")
    parts.append(f"- Publisher rate: {publisher.get('rate_s', '?')}s per printer")
    parts.append(f"- Run duration: {publisher.get('duration_s', '?')}s")
    parts.append(f"- Broker: {publisher.get('broker', '?')}")
    parts.append(f"- Fixture: {publisher.get('fixture', '?')}\n")

    parts.append("## Publisher\n")
    parts.append(f"- Total publishes: {publisher.get('publishes', 0)}")
    parts.append(f"- Failures: {publisher.get('failures', 0)}")
    parts.append(f"- Connect attempts: {publisher.get('connect_attempts', 0)} "
                 f"(successful: {publisher.get('connect_successes', 0)})\n")

    parts.append("## Backend `_on_status` / `_on_message` latency\n")
    if latency:
        summary = summarize_latency(latency)
        parts.append("| function | count | failures | p50 ms | p95 ms | p99 ms | max ms | mean ms |")
        parts.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        for fn in sorted(summary.keys()):
            s = summary[fn]
            parts.append(
                f"| `{fn}` | {s['count']} | {s['failures']} "
                f"| {s['p50_ms']} | {s['p95_ms']} | {s['p99_ms']} "
                f"| {s['max_ms']} | {s['mean_ms']} |"
            )
        parts.append("")
    else:
        parts.append("_No latency.jsonl found — instrumentation may not have been wired._\n")

    parts.append("## DB row growth\n")
    if samples:
        first = samples[0]
        last = samples[-1]
        parts.append("| table | start rows | end rows | delta |")
        parts.append("|---|---:|---:|---:|")
        for table in ("printer_telemetry", "ams_telemetry", "alerts"):
            if table in first and table in last:
                delta = last[table] - first[table]
                parts.append(f"| `{table}` | {first[table]} | {last[table]} | +{delta} |")
        parts.append("")
        parts.append(f"Samples: {len(samples)} (every ~30s)\n")
    else:
        parts.append("_No samples.jsonl found._\n")

    return "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description="Render stress run report")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=None,
                        help="Default: <run-dir>/REPORT.md")
    args = parser.parse_args()

    out = args.out or (args.run_dir / "REPORT.md")
    md = render(args.run_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
