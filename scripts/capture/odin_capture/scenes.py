"""Scene loader — read YAML scene definitions, validate via Pydantic,
expose iteration + lookup helpers used by the CLI.

Wake 1: model definitions, list_scenes(), load_scene(). Validation runs
at load time. No execution path yet — scenes.run() is Wake 2.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

try:
    import yaml  # PyYAML
except ImportError:  # pragma: no cover - surfaced via cli with a clear message
    yaml = None  # type: ignore

try:
    from pydantic import BaseModel, Field, field_validator
except ImportError:  # pragma: no cover
    BaseModel = object  # type: ignore
    Field = lambda *a, **k: None  # type: ignore
    def field_validator(*a, **k):  # type: ignore
        def deco(fn):
            return fn
        return deco

from .config import SCENES_DIR


class ViewportSpec(BaseModel):
    width: int = Field(..., ge=200, le=4000)
    height: int = Field(..., ge=200, le=4000)


class ReplaySpec(BaseModel):
    speed: float = 1.0
    seek: Optional[float] = None


class WaitForSpec(BaseModel):
    selector: str
    min_visible_printers: Optional[int] = None


class CaptureItem(BaseModel):
    name: str
    kind: str  # "screenshot" | "video"
    full_page: bool = False
    duration_sec: Optional[float] = None

    @field_validator("kind")
    @classmethod
    def _kind_known(cls, v: str) -> str:
        if v not in ("screenshot", "video"):
            raise ValueError(f"unknown capture kind: {v!r}")
        return v


class Scene(BaseModel):
    id: str
    title: str
    target_route: str
    scenario: str
    fixture_mode: str = "demo-scenario"
    viewport: ViewportSpec
    theme: str = "dark"
    replay: ReplaySpec = ReplaySpec()
    wait_for: WaitForSpec
    captures: list[CaptureItem]
    formats: list[str] = ["png"]


def list_scene_files() -> list[Path]:
    """All `*.yaml` files in scripts/capture/scenes/. Empty list pre-Wake-2."""
    if not SCENES_DIR.exists():
        return []
    return sorted(SCENES_DIR.glob("*.yaml"))


def load_scene(path: Path) -> Scene:
    """Load + validate a single scene YAML file."""
    if yaml is None:
        raise RuntimeError(
            "PyYAML is required to load scenes. "
            "Run: pip install -r scripts/capture/requirements.txt"
        )
    raw = yaml.safe_load(path.read_text())
    return Scene(**raw)


def list_scenes() -> list[Scene]:
    """All currently-defined scenes, validated. Returns [] in Wake 1."""
    return [load_scene(p) for p in list_scene_files()]
