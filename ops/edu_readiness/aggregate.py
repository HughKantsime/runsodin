"""Derive ODIN's readiness state from policy and gate evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from .common import ResultValidationError, load_result
except ImportError:
    from common import ResultValidationError, load_result


def aggregate(
    run_dir: Path, policy_path: Path, *, scope: str = "full"
) -> dict:
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    all_required = set(policy["required_gates"])
    code_controlled = set(policy["code_controlled_gates"])
    conditional = set(policy["conditionally_blockable_gates"])
    never_conditional = set(policy["never_conditionally_blockable_gates"])
    if not code_controlled <= all_required or not conditional <= all_required or not never_conditional <= all_required:
        raise ResultValidationError("policy gate classes must be subsets of required_gates")
    if scope not in {"full", "code-controlled"}:
        raise ResultValidationError(f"unsupported aggregate scope: {scope}")
    required = code_controlled if scope == "code-controlled" else all_required

    results = {}
    malformed = []
    for path in sorted(run_dir.glob("*.json")):
        if path.name in {"summary.json", "acceptances.json"}:
            continue
        try:
            result = load_result(path)
        except (OSError, json.JSONDecodeError, ResultValidationError) as exc:
            malformed.append(f"{path.name}: {exc}")
            continue
        if result["gate_id"] not in all_required:
            malformed.append(f"{path.name}: undeclared gate_id {result['gate_id']}")
            continue
        if result["gate_id"] in results:
            malformed.append(f"duplicate gate result: {result['gate_id']}")
        results[result["gate_id"]] = result

    missing = sorted(required - results.keys())
    failed = sorted(gate for gate in required if gate in results and results[gate]["status"] == "fail")
    blocked = sorted(gate for gate in required if gate in results and results[gate]["status"] == "blocked")
    invalid_pass = sorted(
        gate for gate in required
        if gate in results and results[gate]["status"] == "pass"
        and (results[gate]["executed_count"] == 0 or results[gate]["skipped_count"] or results[gate]["xfailed_count"])
    )
    acceptances_path = run_dir / "acceptances.json"
    accepted = set()
    if acceptances_path.exists():
        acceptance_data = json.loads(acceptances_path.read_text(encoding="utf-8"))
        for item in acceptance_data.get("accepted_blockers", []):
            if item.get("decision_maker") and item.get("accepted_at"):
                accepted.add(item.get("gate_id"))

    all_pass = not (missing or failed or blocked or invalid_pass or malformed)
    code_pass = all(results.get(g, {}).get("status") == "pass" for g in code_controlled)
    only_accepted_conditional = bool(blocked) and set(blocked) <= conditional and set(blocked) <= accepted
    if all_pass:
        state = "READY"
    elif not missing and not failed and not invalid_pass and not malformed and code_pass and only_accepted_conditional:
        state = "CONDITIONALLY_READY"
    else:
        state = "NOT_READY"

    return {
        "schema_version": 1,
        "scope": scope,
        "status": state,
        "required_gate_count": len(required),
        "passed_gate_count": sum(results.get(g, {}).get("status") == "pass" for g in required),
        "missing_gates": missing,
        "failed_gates": failed,
        "blocked_gates": blocked,
        "invalid_pass_gates": invalid_pass,
        "malformed_results": malformed,
        "never_conditionally_blockable_gates": sorted(never_conditional),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument(
        "--scope", choices=("full", "code-controlled"), default="full",
        help="Evaluate the full production policy or only hermetic code-controlled gates",
    )
    args = parser.parse_args()
    summary = aggregate(args.run_dir, args.policy, scope=args.scope)
    target = args.run_dir / "summary.json"
    target.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(summary["status"])
    return 0 if summary["status"] == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
