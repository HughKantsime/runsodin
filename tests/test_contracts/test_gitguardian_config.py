from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_only_known_fixture_files_are_ignored():
    config = yaml.safe_load((ROOT / ".gitguardian.yaml").read_text())
    assert config["version"] == 2
    paths = config["secret"]["ignored_paths"]
    assert sorted(paths) == [
        "tests/foundry_browser/classic-baseline.json",
        "tests/test_contracts/test_dispatch_recovery_slot_change.py",
    ]
    assert all("*" not in path and (ROOT / path).is_file() for path in paths)
