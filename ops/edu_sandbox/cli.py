"""Operator CLI for isolated ODIN Education sandboxes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .certify import certify_existing
from .errors import SandboxError
from .runtime import DEFAULT_STATE_ROOT, SandboxRuntime


def _print(value: object) -> None:
    if hasattr(value, "to_public_dict"):
        value = value.to_public_dict()
    print(json.dumps(value, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage an isolated ODIN Education sandbox")
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare")
    prepare.add_argument("sandbox_id")
    prepare.add_argument("--allow-dirty", action="store_true")
    prepare.add_argument("--recover", action="store_true")

    request = commands.add_parser("request-license")
    request.add_argument("sandbox_id")

    activate = commands.add_parser("activate")
    activate.add_argument("sandbox_id")
    activate.add_argument("--expires-at", required=True)

    status = commands.add_parser("status")
    status.add_argument("sandbox_id")

    for name in ("reset", "expire", "purge"):
        command = commands.add_parser(name)
        command.add_argument("sandbox_id")
        command.add_argument("--confirm", required=True)
        if name == "reset":
            command.add_argument("--recover", action="store_true")
    for name in ("reconcile", "certify"):
        command = commands.add_parser(name)
        command.add_argument("sandbox_id")
        if name == "certify":
            command.add_argument("--confirm", required=True)

    args = parser.parse_args(argv)
    runtime = SandboxRuntime(args.state_root)
    try:
        if args.command == "prepare":
            _print(runtime.prepare(args.sandbox_id, allow_dirty=args.allow_dirty, recover=args.recover))
        elif args.command == "request-license":
            _print(runtime.request_license(args.sandbox_id, sys.stdin.buffer, is_tty=sys.stdin.isatty()))
        elif args.command == "activate":
            _print(runtime.activate(args.sandbox_id, sys.stdin.buffer, is_tty=sys.stdin.isatty(), expires_at=args.expires_at))
        elif args.command == "status":
            _print(runtime.status(args.sandbox_id))
        elif args.command == "expire":
            _print(runtime.expire(args.sandbox_id, confirm=args.confirm))
        elif args.command == "reconcile":
            _print(runtime.reconcile(args.sandbox_id))
        elif args.command == "reset":
            _print(runtime.reset(args.sandbox_id, confirm=args.confirm, recover=args.recover))
        elif args.command == "purge":
            _print(runtime.purge(args.sandbox_id, confirm=args.confirm))
        elif args.command == "certify":
            code, report = certify_existing(
                args.sandbox_id,
                confirm=args.confirm,
                state_root=args.state_root,
                artifact_root=Path("artifacts/edu-sandbox"),
            )
            _print({"exit_code": code, "report": str(report)})
            return code
        else:
            raise SandboxError(f"{args.command} is not implemented yet")
    except SandboxError as exc:
        print(f"ODIN EDU sandbox error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
