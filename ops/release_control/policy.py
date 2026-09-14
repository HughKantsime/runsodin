"""Static policy validators shared by implementation and tests."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .run_gate import BASE_ENVIRONMENT_ALLOWLIST

ROOT = Path(__file__).parents[2]
INVENTORY = Path(__file__).with_name("inventory.json")
EXPECTED_CASES = [
    *(f"WF{i:02d}" for i in range(1, 13)),
    *(f"RS{i:02d}" for i in range(1, 13)),
    *(f"IN{i:02d}" for i in range(1, 16)),
    *(f"RE{i:02d}" for i in range(1, 11)),
]
EXPECTED_COMPONENTS = [f"CV{i:02d}" for i in range(1, 11)]


def load_inventory() -> dict[str, object]:
    data = json.loads(INVENTORY.read_text(encoding="utf-8"))
    cases = data.get("acceptance_cases", [])
    components = data.get("components", [])
    case_ids = [item.get("id") for item in cases]
    component_ids = [item.get("id") for item in components]
    if case_ids != EXPECTED_CASES:
        raise ValueError("acceptance inventory IDs/order are not exact")
    if component_ids != EXPECTED_COMPONENTS:
        raise ValueError("component inventory IDs/order are not exact")
    if data.get("base_environment_allowlist") != list(BASE_ENVIRONMENT_ALLOWLIST):
        raise ValueError("base environment allowlist is not exact")
    for item in components:
        required = (
            "command", "working_directory", "environment_allowlist", "timeout_seconds",
            "expected_artifacts", "json_status", "source", "result_kind",
        )
        if any(key not in item for key in required):
            raise ValueError(f"incomplete component inventory: {item.get('id')}")
        if item["working_directory"] != "." or not item["command"] or not item["expected_artifacts"]:
            raise ValueError(f"invalid component inventory: {item.get('id')}")
        allowlist = item["environment_allowlist"]
        if not isinstance(allowlist, list) or len(allowlist) != len(set(allowlist)):
            raise ValueError(f"invalid component environment allowlist: {item.get('id')}")
    return data


def workflow_text() -> str:
    return (ROOT / ".github/workflows/trusted-validation.yml").read_text(encoding="utf-8")


def workflow_events(source: str) -> set[str]:
    match = re.search(r"(?m)^on:\s*\n(?P<body>(?:^[ ]{2}.*\n?)*)", source)
    if not match:
        raise ValueError("workflow has no event map")
    return set(re.findall(r"(?m)^  ([a-zA-Z_]+):", match.group("body")))


def dispatch_allowed(actor: str, actor_id: str, triggering_actor: str, attempt: int) -> bool:
    return (
        actor == "HughKantsime"
        and actor_id == "201174638"
        and triggering_actor == actor
        and attempt == 1
    )


def valid_candidate(target_sha: str, target_ref: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{40}", target_sha)) and target_ref == f"release-candidate/{target_sha}"
