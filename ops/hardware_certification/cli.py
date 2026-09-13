"""Operator CLI for separated replay, observe, authorization, and verification flows."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from .artifact import ROOT, git_identity, publish_result
from .assets import inspect_test_asset
from .authorization import create_template
from .config import DEFAULT_ARTIFACT_ROOT, load_target
from .evidence import verify_artifact
from .security import assert_protected_output_path


def _run_id() -> str:
    commit, _dirty = git_identity()
    return f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{commit}"


def _write_exclusive(path: Path, payload: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _authorize(args: argparse.Namespace) -> int:
    target = load_target(args.target_config, artifact_root=args.artifact_root)
    assert_protected_output_path(args.output, args.artifact_root)
    actions = [item.strip() for item in args.actions.split(",") if item.strip()]
    asset_sha = None
    if args.asset:
        _path, _extension, asset_sha = inspect_test_asset(
            args.asset, protocol=target.protocol, repository_root=ROOT,
            artifact_root=args.artifact_root,
        )
    template = create_template(
        args.run_id or _run_id(), args.target_config, actions,
        test_asset_sha256=asset_sha, elegoo_filename=args.elegoo_filename,
    )
    _write_exclusive(args.output, template)
    print(args.output)
    return 0


def _observe(args: argparse.Namespace) -> int:
    from .passive.live import observe_target_file

    result = observe_target_file(args.target_config, artifact_root=args.artifact_root)
    run_dir = publish_result(result, args.artifact_root)
    print(run_dir / "index.html")
    return 0 if result["status"] == "pass" else 2


def _verify(args: argparse.Namespace) -> int:
    manifest, results = verify_artifact(
        args.run_dir, expected_mode=args.mode,
        expected_protocol=args.protocol,
        expected_model_family=args.model_family,
        expected_firmware_version=args.firmware_version,
        expected_api_version=args.api_version,
    )
    print(json.dumps({
        "run_id": manifest["run_id"], "mode": manifest["mode"],
        "protocol": manifest["protocol"], "status": manifest["status"],
        "verified_protocols": sorted(results),
    }, sort_keys=True))
    return 0


def _report(args: argparse.Namespace) -> int:
    verify_artifact(args.run_dir)
    report = args.run_dir / "index.html"
    if not report.is_file():
        raise RuntimeError("verified artifact has no HTML report")
    print(report)
    return 0


def _exercise(args: argparse.Namespace) -> int:
    from .live_exercise import execute_live_exercise

    run_dir = execute_live_exercise(
        authorization_path=args.authorization, target_path=args.target_config,
        ledger_path=args.ledger, artifact_root=args.artifact_root, asset_path=args.asset,
    )
    print(run_dir / "index.html")
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    return 0 if manifest["status"] == "pass" else 2


def _telemetry_prerequisites(args: argparse.Namespace) -> int:
    from .telemetry_prerequisites import build_prerequisites

    build_prerequisites(
        observe_artifact=args.observe_artifact,
        exercise_artifact=args.exercise_artifact, output=args.output,
    )
    print(args.output)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    replay = commands.add_parser("replay", help="run loopback-only protocol certification")
    replay.set_defaults(handler=lambda _args: __import__("ops.hardware_certification.runner", fromlist=["main"]).main())

    observe = commands.add_parser("observe", help="collect passive real-device evidence")
    observe.add_argument("--target-config", type=Path, required=True)
    observe.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT / "hardware-certification")
    observe.set_defaults(handler=_observe)

    authorize = commands.add_parser("authorize-template", help="create a default-deny one-time active template")
    authorize.add_argument("--target-config", type=Path, required=True)
    authorize.add_argument("--actions", required=True, help="comma-separated exact action names")
    authorize.add_argument("--output", type=Path, required=True)
    authorize.add_argument("--run-id")
    authorize.add_argument("--asset", type=Path)
    authorize.add_argument("--elegoo-filename")
    authorize.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    authorize.set_defaults(handler=_authorize)

    exercise = commands.add_parser("exercise", help="consume authorization and run exact active actions")
    exercise.add_argument("--authorization", type=Path, required=True)
    exercise.add_argument("--target-config", type=Path, required=True)
    exercise.add_argument("--ledger", type=Path, required=True)
    exercise.add_argument("--asset", type=Path)
    exercise.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT / "hardware-certification")
    exercise.set_defaults(handler=_exercise)

    verify = commands.add_parser("verify-artifact", help="verify schema, hashes, privacy, freshness, and identity")
    verify.add_argument("run_dir", type=Path)
    verify.add_argument("--mode", choices=("replay", "observe", "exercise"))
    verify.add_argument("--protocol", choices=("bambu", "elegoo", "moonraker", "prusalink"))
    verify.add_argument("--model-family")
    verify.add_argument("--firmware-version")
    verify.add_argument("--api-version")
    verify.set_defaults(handler=_verify)

    report = commands.add_parser("report", help="print the HTML path for a verified artifact")
    report.add_argument("run_dir", type=Path)
    report.set_defaults(handler=_report)

    telemetry = commands.add_parser("telemetry-prerequisites", help="derive evidence-linked V2 checklist rows")
    telemetry.add_argument("--observe-artifact", type=Path, required=True)
    telemetry.add_argument("--exercise-artifact", type=Path)
    telemetry.add_argument("--output", type=Path, required=True)
    telemetry.set_defaults(handler=_telemetry_prerequisites)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except KeyboardInterrupt:
        print("hardware certification interrupted", file=sys.stderr)
        return 130
    except Exception:
        command = str(args.command).replace("-", "_")
        print(f"hardware certification failed: {command}_failed", file=sys.stderr)
        return 2
