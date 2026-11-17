"""Profile the Education HTTP workload locally; print JSON or text, never reports.

Run in a fresh process: python3.11 -B ops/edu_readiness/request_profile.py
Use --tree /path/to/detached/v2.0.45 to run this same instrument against an old
checkout. --compare /path/to/detached/v2.0.45 runs both in fresh child processes.
Each workload runs in one process, with 25 concurrent users and three repetitions.
--sequential issues one request at a time and measures active wall/CPU spans.
--statements lists SQL callers from one warm-up request per class (no parameters).
--smoke runs just 50 measured operations once, after the usual warm-up.
The shared fixture creates and removes a disposable database and signed licence.
No lifespan, WebSocket cycles, production services, or network sockets are used.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path
from statistics import mean

try:
    from .request_profile_layers import Profile, source_name
except ImportError:
    from request_profile_layers import Profile, source_name


ROOT = Path(__file__).resolve().parents[2]
CLASSES = ("auth", "printers", "jobs", "reports", "job_create", "job_approve", "session_churn")


def load_workload(tree):
    """Import the target tree's fixture, not whichever backend is on PYTHONPATH."""
    if "core.db" in sys.modules or "license_manager" in sys.modules:
        raise RuntimeError("Run the profiler in a fresh Python process")
    sys.path[:0] = [str(tree / "backend"), str(tree / "ops" / "edu_readiness")]
    spec = importlib.util.spec_from_file_location("profile_workload", tree / "ops/edu_readiness/api_load.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def install(profile, app):
    """Patch existing call sites, including FastAPI's captured dependency objects."""
    import jwt
    import core.dependencies as dependencies

    aliases = {}
    targets = {
        "get_current_user": "auth.user_resolution",
        "_resolve_current_user": "auth.user_resolution",
        "resolve_current_user_sync": "auth.user_resolution",
        "validate_access_token": "auth.user_resolution",
        "validate_perimeter_access_token": "auth.token_session",
        "resolve_user_api_token": "auth.user_resolution",
        "resolve_legacy_api_key": "auth.user_resolution",
        "_principal": "auth.user_resolution",
        "decode_token": "auth.token_session",
        "token_predates_account": "auth.token_session",
        "_should_write_last_seen": "auth.token_session",
        "_touch_session_last_seen": "auth.token_session",
    }
    for name, label in targets.items():
        if hasattr(dependencies, name):
            original, replacement = profile.wrap(dependencies, name, label)
            aliases[original] = replacement
    profile.wrap(jwt, "decode", "auth.token_session")
    for module_name, names, label in (
        ("core.controlled_parts", ("any_read_restrictions",), "auth.read_restrictions"),
        ("core.controlled_request", ("authorize_request", "authorize_references", "guard_source_changes"), "auth.controlled_authorization"),
        ("core.license_lock", ("authorize_user",), "auth.license_policy"),
    ):
        try:
            module = __import__(module_name, fromlist=["*"])
        except ModuleNotFoundError as exc:
            if exc.name != module_name:
                raise
            continue
        for name in names:
            if not hasattr(module, name):
                continue
            original, replacement = profile.wrap(module, name, label)
            aliases[original] = replacement
    # Already-imported routes and helpers retain from-import aliases.
    for name, module in list(sys.modules.items()):
        if name.startswith(("core.", "modules.")) and module is not None:
            for attr, value in list(vars(module).items()):
                if callable(value):
                    try:
                        replacement = aliases.get(value)
                    except TypeError:
                        continue
                    if replacement is not None:
                        profile.patch(module, attr, replacement)

    install_framework(profile, app, aliases)


def install_framework(profile, app, aliases=None):
    import fastapi.routing
    from starlette.responses import JSONResponse

    aliases = aliases or {}

    def dependency(node):
        replacement = aliases.get(node.call)
        if replacement is not None:
            profile.patch(node, "call", replacement)
        for child in node.dependencies:
            dependency(child)

    route_sources = set()
    for route in app.routes:
        if isinstance(route, fastapi.routing.APIRoute):
            dependency(route.dependant)
            try:
                category(next(iter(route.methods)), route.path)
            except ValueError:
                pass
            else:
                function = route.dependant.call
                route_sources.add(source_name(function))
            profile.wrap(route.dependant, "call", "route_handler")
    profile.sources["route_handler"] = "; ".join(sorted(route_sources)) or "FastAPI APIRoute.dependant.call"
    profile.wrap(fastapi.routing, "serialize_response", "response_serialization")
    profile.wrap(JSONResponse, "render", "response_serialization")
    profile.patch(app, "middleware_stack", profile.middleware(app.build_middleware_stack()))


def category(method, url):
    path = str(url).split("?", 1)[0]
    if path.endswith("/logout"):
        return "session_churn"
    if path.endswith("/approve"):
        return "job_approve"
    if path.endswith("/jobs"):
        return "job_create" if method.upper() == "POST" else "jobs"
    if path.endswith("/printers"):
        return "printers"
    if path.endswith(("/quotas", "/usage-report")):
        return "reports"
    if path.endswith(("/me", "/permissions")):
        return "auth"
    raise ValueError("Operation is not in the Education HTTP workload")


def summarize(runs):
    tables = [run["table"] for run in runs]
    result = {}
    for kind in tables[0]:
        rows = [table[kind] for table in tables]
        labels = set().union(*(row["layers_ms"] for row in rows))
        sql_owners = set().union(*(row.get("sql_by_layer_ms", {}) for row in rows))
        result[kind] = {
            "requests": sum(row["requests"] for row in rows),
            "total_ms": mean(row["total_ms"] for row in rows),
            "total_range_ms": [min(row["total_ms"] for row in rows), max(row["total_ms"] for row in rows)],
            "statements": mean(row["statements"] for row in rows),
            "sql_ms_inclusive": mean(row["sql_ms_inclusive"] for row in rows),
            "sql_by_layer_ms": {label: mean(row.get("sql_by_layer_ms", {}).get(label, 0) for row in rows)
                                for label in sorted(sql_owners)},
            "layers_ms": {label: mean(row["layers_ms"].get(label, 0) for row in rows) for label in sorted(labels)},
            "layer_ranges_ms": {label: [min(row["layers_ms"].get(label, 0) for row in rows),
                                        max(row["layers_ms"].get(label, 0) for row in rows)] for label in sorted(labels)},
        }
    return result


def smoke_workload(operations):
    """Keep the shuffled mix, with proportional class counts rounded to 50."""
    counts = Counter(operation.category for operation in operations)
    quotas = {kind: counts[kind] * 50 // len(operations) for kind in CLASSES}
    remainder = sorted(CLASSES, key=lambda kind: -(counts[kind] * 50 % len(operations)))
    for kind in remainder[:50 - sum(quotas.values())]:
        quotas[kind] += 1
    selected = []
    for operation in operations:
        if quotas[operation.category]:
            selected.append(operation)
            quotas[operation.category] -= 1
    return selected


async def run(tree, sequential=False, statements=False, smoke=False):
    workload = load_workload(tree)
    import httpx

    with tempfile.TemporaryDirectory(prefix="odin-request-profile-") as directory:
        os.environ["ODIN_AUDIT_KEY_DIR"] = str(Path(directory) / "audit")
        app, tokens = workload._prepare_database(Path(directory) / "odin.db")
        from core.db import engine
        profile = Profile(sequential=sequential)
        concurrency = 1 if sequential else 25
        try:
            install(profile, app)
            original = httpx.AsyncClient.request
            warmup_operations = workload.warmup_workload()
            trace_remaining = Counter(operation.category for operation in warmup_operations) if statements else Counter()
            traces = {}

            async def measured(client, method, url, *args, **kwargs):
                kind = category(method, url)
                capture = trace_remaining[kind] == 1
                trace_remaining[kind] -= 1
                with profile.request(kind, statements=capture) as sample:
                    response = await original(client, method, url, *args, **kwargs)
                if capture:
                    traces[kind] = {"phase": "warmup", "method": method, "path": str(url),
                                    "status": response.status_code, "statements": sample.sql_trace}
                return response

            profile.patch(httpx.AsyncClient, "request", measured)
            runs = []
            samples = []
            with profile.database(engine):
                _, errors, leaks, _ = await workload._execute_repetition(app, tokens, warmup_operations, concurrency)
                warmup = {"errors": errors, "tenant_leaks": leaks}
                for number in range(1 if smoke else 3):
                    profile.samples.clear()
                    current_tokens = dict(tokens, churn=tokens["churn"][number * 50:(number + 1) * 50])
                    operations = workload.workload(list(range(10000 + number * 75, 10075 + number * 75)), number)
                    if smoke:
                        operations = smoke_workload(operations)
                    _, errors, leaks, duration = await workload._execute_repetition(app, current_tokens, operations, concurrency)
                    runs.append({"repetition": number + 1, "duration_seconds": duration,
                                 "errors": errors, "tenant_leaks": leaks, "table": profile.table()})
                    samples.extend(profile.samples)
            result = {"schema_version": 2, "tree": str(tree), "python": sys.version.split()[0],
                    "fastapi": __import__("fastapi").__version__, "concurrency": concurrency, "smoke": smoke,
                    "warmup": warmup, "runs": runs, "mean": summarize(runs), "sources": profile.sources,
                    "notes": ["Exclusive active wall and process CPU time; async suspension belongs to framework_wait."
                              if sequential else "Exclusive elapsed-time partition; suspension is included, not CPU time.",
                              "Framework wait includes routing, dependency scheduling, transport and profiling overhead.",
                              "Database events time cursor execution, not fetching, commit, pool wait or ORM conversion.",
                              "Inclusive SQL is diagnostic: do not add it to layers_ms.",
                              "Repetitions share one fixture, like api_load; no WebSocket or lifespan work.",
                              "The fixture has no restricted parts; authorization/redaction costs with restricted data are not measured.",
                              "API key perimeter and fleet-lock enforcement are off, matching clean fixture defaults.",
                              "Changes below 5% of the before value are noise, not evidence of regression."]}
            if sequential:
                result["statistics"] = profile.distributions(samples)
                result["notes"].extend([
                    "Percentiles use individual measured requests pooled across repetitions; layer medians are not additive.",
                    "Process CPU includes all process threads; overlapping work is partitioned, not thread-local CPU.",
                    "Synchronous blocking inside a measured function remains part of its wall time.",
                ])
            if statements:
                result["statement_requests"] = traces
                result["notes"].append("SQL lists come from the last warm-up request per class; stack inspection is outside measured runs.")
            if smoke:
                result["notes"].append("50-operation smoke check only; not a performance measurement.")
            return result
        finally:
            profile.restore()
            engine.dispose()


def compare(before, after):
    for result in (before, after):
        if "runs" in result and (result["warmup"]["errors"] or result["warmup"]["tenant_leaks"]
                                 or any(run["errors"] or run["tenant_leaks"] for run in result["runs"])):
            raise ValueError("Cannot rank a workload with unexpected responses or tenant leaks")
    left, right = before["mean"]["all"], after["mean"]["all"]
    delta = right["total_ms"] - left["total_ms"]
    ranked = []
    for label in left["layers_ms"].keys() | right["layers_ms"].keys():
        old, new = left["layers_ms"].get(label, 0), right["layers_ms"].get(label, 0)
        change = new - old
        ranked.append({"layer": label, "before_ms": old, "after_ms": new, "delta_ms": change,
                       "percent_of_total_delta": change / delta * 100 if delta else None,
                       "percent_change": change / old * 100 if old else None,
                       "under_five_percent": abs(change) < abs(old) * .05 if old else None,
                       "new": label not in before["sources"],
                       "source": after["sources"].get(label, before["sources"].get(label))})
    return {"before": before, "after": after,
            "total_delta_ms": delta, "ranked": sorted(ranked, key=lambda row: row["delta_ms"], reverse=True)}


def render_text(result):
    if "before" in result:
        lines = ["BEFORE", render_text(result["before"]), "AFTER", render_text(result["after"]),
                 "Layer | before ms | after ms | change ms | % of total change | source"]
        for row in result["ranked"]:
            share = row["percent_of_total_delta"]
            note = " NEW" if row["new"] else " noise (<5%)" if row["under_five_percent"] else ""
            lines.append(f"{row['layer']}{note} | {row['before_ms']:.3f} | {row['after_ms']:.3f} | "
                         f"{row['delta_ms']:.3f} | {share:.1f}% | {row['source']}" if share is not None else row["layer"])
        return "\n".join(lines)
    lines = [f"Tree: {result['tree']}", "Repetition seconds: " + ", ".join(f"{r['duration_seconds']:.3f}" for r in result["runs"]),
             f"Warmup errors/leaks: {len(result['warmup']['errors'])}/{result['warmup']['tenant_leaks']}",
             "Measured errors/leaks: " + ", ".join(f"{len(r['errors'])}/{r['tenant_leaks']}" for r in result["runs"])]
    kinds = (*CLASSES, "all")
    if "statistics" in result:
        lines.append("Median [p10, p90] per request | " + " | ".join(kinds))

        def cells(values):
            return " | ".join(f"{value['median']:.3f} [{value['p10']:.3f}, {value['p90']:.3f}]" for value in values)

        table = result["statistics"]
        lines.append("Requests | " + " | ".join(str(table[kind]["requests"]) for kind in kinds))
        for metric in ("wall_ms", "cpu_ms", "statements", "sql_ms"):
            lines.append(metric + " | " + cells(table[kind][metric] for kind in kinds))
        for metric in ("layers_wall_ms", "layers_cpu_ms"):
            for label in table["all"][metric]:
                lines.append(f"{metric}.{label} | " + cells(table[kind][metric][label] for kind in kinds))
        lines.extend(render_statements(result))
        lines.extend(result["notes"])
        return "\n".join(lines)
    lines.append("Mean milliseconds per request | " + " | ".join(kinds))
    for label in result["mean"]["all"]["layers_ms"]:
        lines.append(label + " | " + " | ".join(f"{result['mean'][kind]['layers_ms'].get(label, 0):.3f}" for kind in kinds))
    for metric in ("total_ms", "statements", "sql_ms_inclusive"):
        lines.append(metric + " | " + " | ".join(f"{result['mean'][kind][metric]:.3f}" for kind in kinds))
    lines.append("Total range ms | " + " | ".join(
        f"{result['mean'][kind]['total_range_ms'][0]:.3f}–{result['mean'][kind]['total_range_ms'][1]:.3f}" for kind in kinds))
    lines.append("SQL by caller (included above; do not add again)")
    for label in result["mean"]["all"]["sql_by_layer_ms"]:
        lines.append(label + " | " + " | ".join(
            f"{result['mean'][kind]['sql_by_layer_ms'].get(label, 0):.3f}" for kind in kinds))
    lines.extend(render_statements(result))
    lines.extend(result["notes"])
    return "\n".join(lines)


def render_statements(result):
    lines = []
    for kind in CLASSES:
        request = result.get("statement_requests", {}).get(kind)
        if request is None:
            continue
        lines.append(f"SQL {kind}: {request['method']} {request['path']} ({request['phase']}, status {request['status']})")
        for index, statement in enumerate(request["statements"], 1):
            lines.append(f"{index}. {statement['source']} | {statement['statement']}")
    return lines


def isolate():
    """Standalone-only guard: no installation settings, .env reads or sockets."""
    safe = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "TMPDIR") if key in os.environ}
    os.environ.clear()
    os.environ.update(safe, PYTHONDONTWRITEBYTECODE="1", API_KEY="")
    sys.dont_write_bytecode = True
    logging.disable(logging.CRITICAL)

    def audit(event, args):
        if event in {"socket.connect", "socket.getaddrinfo", "socket.bind"}:
            raise RuntimeError("Request profiling cannot use network sockets")
        if event == "open" and isinstance(args[0], (str, bytes)):
            name = os.fsdecode(args[0]).rsplit("/", 1)[-1]
            if name == ".env" or name.startswith(".env."):
                raise RuntimeError("Request profiling cannot read environment files")
    sys.addaudithook(audit)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", type=Path, default=ROOT)
    parser.add_argument("--compare", type=Path, help="Existing detached checkout to compare with --tree")
    parser.add_argument("--format", choices=("json", "text"), default="text")
    parser.add_argument("--sequential", action="store_true", help="Issue one request at a time; measure active wall and CPU time")
    parser.add_argument("--statements", action="store_true", help="List SQL and callers from one warm-up request per class")
    parser.add_argument("--smoke", action="store_true", help="Run only 50 measured operations once after warm-up")
    args = parser.parse_args()
    isolate()
    if args.compare:
        results = []
        options = [flag for flag, enabled in (("--sequential", args.sequential), ("--statements", args.statements),
                                              ("--smoke", args.smoke)) if enabled]
        for tree in (args.compare, args.tree):
            child = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--tree", str(tree.resolve()),
                                    "--format", "json", *options], capture_output=True, text=True, check=True)
            results.append(json.loads(child.stdout))
        result = compare(*results)
    else:
        # Bootstrap prints progress; keep it out of JSON without creating a log.
        with redirect_stdout(io.StringIO()):
            result = asyncio.run(run(args.tree.resolve(), args.sequential, args.statements, args.smoke))
    print(json.dumps(result, indent=2) if args.format == "json" else render_text(result))


if __name__ == "__main__":
    main()
