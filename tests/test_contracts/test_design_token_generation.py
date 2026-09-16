from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_design_token_source_reproduces_tracked_accessible_css(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "node is required to verify generated design tokens"

    workspace = tmp_path / "workspace"
    shutil.copytree(ROOT / "design", workspace / "design")
    output_dir = workspace / "frontend" / "src"
    output_dir.mkdir(parents=True)

    subprocess.run(
        [node, str(workspace / "design" / "generate.mjs"), "--local-only"],
        check=True,
        text=True,
        capture_output=True,
    )

    generated = (output_dir / "design-tokens.css").read_bytes()
    tracked = (ROOT / "frontend" / "src" / "design-tokens.css").read_bytes()
    assert generated == tracked

    tokens = json.loads((ROOT / "design" / "tokens.json").read_text(encoding="utf-8"))
    assert tokens["brand"]["onPrimary"] == "#0B0D11"
    assert tokens["text"]["dark"]["muted"] == "#9099AA"
    assert tokens["modes"]["highContrast"]["brand"]["onPrimary"] == "#080A0E"
    assert tokens["modes"]["highContrast"]["text"]["muted"] == "#AAB4C8"
    assert tokens["modes"]["light"]["brand"]["onPrimary"] == "#FFFFFF"
    assert tokens["modes"]["light"]["text"]["muted"] == "#607086"
