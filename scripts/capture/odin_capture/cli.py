"""odin_capture CLI — `python -m scripts.capture.odin_capture.cli`.

Subcommands:
  list-scenes   Print scene id + title for every YAML in scripts/capture/scenes/.
  validate      Validate a scene YAML without running it.
  scene <id>    Run a scene end-to-end.

Wake 1 acceptance: `python -m scripts.capture.odin_capture.cli list-scenes`
runs without crash.
Wake 2 acceptance: scene-runner orchestration is wired (boot stack → bootstrap
printers → login → capture). Hits a Wake-2b halt at `bootstrap_scenario_printers`
on `--target local`; `--target demo` is blocked behind a separate flag.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .auth import bootstrap_or_login
from .browser import Viewport, capture_screenshot
from .config import resolve_target, TARGETS, DEFAULT_OUTPUT_DIR
from .processes import (
    StackError,
    bootstrap_scenario_printers,
    start_local_stack,
    stop_local_stack,
    wait_for_health,
)
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


def _parse_viewport(spec: str) -> Viewport:
    try:
        w, h = spec.lower().split("x", 1)
        return Viewport(width=int(w), height=int(h))
    except (ValueError, AttributeError) as e:
        raise SystemExit(f"error: --viewport expected WIDTHxHEIGHT, got {spec!r}: {e}")


def _cmd_scene(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        target = resolve_target(args.target)
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # Locate + load the scene
    scene_files = list_scene_files()
    match = next((p for p in scene_files if p.stem == args.scene), None)
    if match is None:
        print(
            f"error: scene {args.scene!r} not found. "
            f"Run `list-scenes` to see what's defined.",
            file=sys.stderr,
        )
        return 2
    scene = load_scene(match)

    # CLI overrides
    scenario = args.scenario or scene.scenario
    viewport = (
        _parse_viewport(args.viewport)
        if args.viewport else
        Viewport(width=scene.viewport.width, height=scene.viewport.height)
    )
    out_dir = Path(args.out) if args.out else DEFAULT_OUTPUT_DIR
    formats = [f.strip() for f in args.output.split(",") if f.strip()]

    # Currently we only emit PNG; MP4/GIF land in Wake 3.
    unknown_formats = [f for f in formats if f not in ("png",)]
    if unknown_formats:
        print(
            f"warning: requested format(s) {unknown_formats} not yet implemented "
            f"(Wake 3 deliverable); only PNG will be emitted.",
            file=sys.stderr,
        )

    if target.name == "local":
        return _run_local(scene, scenario, target.base_url, viewport, out_dir)
    if target.name == "demo":
        return _run_demo(scene, scenario, target.base_url, viewport, out_dir)
    print(f"error: target {target.name!r} not implemented", file=sys.stderr)
    return 2


def _run_local(scene, scenario, base_url, viewport, out_dir) -> int:
    handle = None
    try:
        handle = start_local_stack(scenario)
        # Wake 2b lands the printer-seeding step. Until then this raises
        # StackError with a clear pointer at ODIN-142.
        bootstrap_scenario_printers(base_url, scenario, access_token="")
        cookie = bootstrap_or_login(base_url)

        out_path = out_dir / scene.id / f"{scene.id}.png"
        capture_screenshot(
            base_url=base_url,
            route=scene.target_route,
            viewport=viewport,
            cookie=cookie,
            wait_selector=scene.wait_for.selector,
            out_path=out_path,
            full_page=any(c.full_page for c in scene.captures if c.kind == "screenshot"),
            min_visible_printers=scene.wait_for.min_visible_printers,
        )
        print(str(out_path))
        return 0
    except StackError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        if handle is not None:
            stop_local_stack(handle)


def _run_demo(scene, scenario, base_url, viewport, out_dir) -> int:
    """Capture against the live demo.subsystem.app deployment.

    Useful for "what App Reviewers see" smoke captures. Not deterministic
    — the live demo's replay phase isn't synchronized with the local clock,
    and the App Reviewer account scoping (`x1c-demo` only) means
    fleet-overview-style multi-printer captures are not possible here.

    Wake 4 wires this fully (smoke-test + Reddit-thumb fast-path);
    Wake 2 is a stub.
    """
    print(
        "error: --target demo is a Wake 4 deliverable "
        "(App Review scoping limits multi-printer scenes anyway).",
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
