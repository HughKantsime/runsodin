"""odin_capture CLI — `python -m scripts.capture.odin_capture.cli`.

Subcommands:
  list-scenes   Print scene id + title for every YAML in scripts/capture/scenes/.
  validate      Validate a scene YAML without running it.
  scene <id>    Run a scene end-to-end (Wake 2+).

Wake 1 acceptance: `python -m scripts.capture.odin_capture.cli list-scenes`
runs without crash. The `scene` subcommand is wired for argparse completeness
but exits non-zero with a clear message until Wake 2 lands.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import resolve_target, TARGETS
from .scenes import list_scenes, load_scene, list_scene_files


def _cmd_list_scenes(args: argparse.Namespace) -> int:
    scenes = list_scenes()
    if not scenes:
        print("(no scenes registered yet — Wake 2 ships fleet-overview.yaml)")
        return 0
    for s in scenes:
        print(f"{s.id}\t{s.title}\t(scenario={s.scenario})")
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.exists():
        print(f"error: scene file not found: {path}", file=sys.stderr)
        return 2
    try:
        s = load_scene(path)
    except Exception as exc:
        print(f"validation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"ok: {s.id} ({s.title})")
    return 0


def _cmd_scene(args: argparse.Namespace) -> int:
    # Validate the target up-front so a bad --target fails before Wake 2 work.
    try:
        resolve_target(args.target)
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # Find the scene by id.
    scene_files = list_scene_files()
    match = next(
        (p for p in scene_files if p.stem == args.scene),
        None,
    )
    if match is None:
        print(
            f"error: scene {args.scene!r} not found. "
            f"Run `list-scenes` to see what's defined.",
            file=sys.stderr,
        )
        return 2

    print(
        "scene runner: Wake 2 deliverable. "
        "Wake 1 only ships scaffolding + scene loading. "
        f"Validated scene file: {match}",
        file=sys.stderr,
    )
    return 3


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="odin_capture",
        description=(
            "ODIN marketing-asset capture pipeline. "
            "Real fixture replay + live UI render → PNG/MP4/GIF outputs."
        ),
    )
    p.add_argument("--version", action="version", version=f"odin_capture {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    sub_list = sub.add_parser("list-scenes", help="Print all defined scenes.")
    sub_list.set_defaults(func=_cmd_list_scenes)

    sub_validate = sub.add_parser("validate", help="Validate a scene YAML file.")
    sub_validate.add_argument("path", help="Path to a scene .yaml file.")
    sub_validate.set_defaults(func=_cmd_validate)

    sub_scene = sub.add_parser("scene", help="Run a scene end-to-end.")
    sub_scene.add_argument("scene", help="Scene id (matches scenes/<id>.yaml).")
    sub_scene.add_argument(
        "--target",
        default="local",
        choices=sorted(TARGETS),
        help="Backend target. local boots stack itself; demo points at demo.subsystem.app.",
    )
    sub_scene.add_argument("--scenario", default=None, help="Override the scene's scenario.")
    sub_scene.add_argument("--replay-speed", type=float, default=None, help="Override replay speed.")
    sub_scene.add_argument("--viewport", default=None, help="WIDTHxHEIGHT override.")
    sub_scene.add_argument(
        "--output",
        default="png",
        help="Comma-separated formats: png,mp4,gif",
    )
    sub_scene.add_argument("--out", default=None, help="Output directory.")
    sub_scene.set_defaults(func=_cmd_scene)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
