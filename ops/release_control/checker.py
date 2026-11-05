"""Disposable, loopback-only smoke of the exact published OCI index (no fleet data)."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
import urllib.request
from pathlib import Path

from .validation_image_cleanup import validation_capacity_observation

IMAGE = "ghcr.io/hughkantsime/odin"
MACHINE = {"linux/amd64": "x86_64", "linux/arm64": "aarch64"}
PLATFORMS = ("linux/amd64", "linux/arm64")


class CheckerError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(url):
    # Never send loopback traffic through a runner's HTTP proxy or follow redirects.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(url, timeout=10) as response:
        if response.status != 200:
            raise CheckerError("smoke requires HTTP 200")
        return response.headers.get_content_type(), response.read(2 * 1024 * 1024)


def document(get, base, path):
    content_type, body = get(base + path)
    if content_type != "application/json":
        raise CheckerError(f"{path} did not return JSON")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise CheckerError(f"{path} did not return an object")
    return value


def smoke(base, version, *, get=fetch):
    health = document(get, base, "/health")
    ready = document(get, base, "/health/ready")
    if health.get("status") != "ok" or health.get("version") != version:
        raise CheckerError("health status/version mismatch")
    if ready.get("ready") is not True or ready.get("version") != version:
        raise CheckerError("readiness/version mismatch")
    if document(get, base, "/api/version").get("version") != version:
        raise CheckerError("API version mismatch")
    setup = document(get, base, "/api/setup/status")
    if setup.get("needs_setup") is not True or setup.get("has_users") is not False or setup.get("is_complete") is not False:
        raise CheckerError("fresh volume must need setup and contain no users")
    for path in ("/login", "/setup"):
        content_type, body = get(base + path)
        if content_type != "text/html" or b'id="root"' not in body or b'<script' not in body:
            raise CheckerError(f"{path} did not serve the application")
    return {"status": "ok", "version": version, "ready": True,
            "smoke": {"login": True, "api_version": True, "fresh_setup": True}}


def docker(*args):
    result = subprocess.run(["docker", *args], text=True, capture_output=True, timeout=300)
    if result.returncode:
        # Docker output may include application secrets: do not embed it in evidence/logs.
        raise CheckerError(f"docker {args[0]} failed")
    return result.stdout.strip()


def capacity():
    record, error = validation_capacity_observation()
    if error or not record["passed"]:
        raise CheckerError("Docker capacity probe failed or free space below 6 GiB")


def run_checker(digest, version, name, runner, *, call=docker, get=fetch,
                check_capacity=capacity, clock=time.monotonic, sleep=time.sleep, timeout=600):
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest) or not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version):
        raise CheckerError("invalid digest/version")
    if not re.fullmatch(r"odin-checker-[1-9][0-9]*-1", name) or not runner.strip():
        raise CheckerError("invalid checker identity")
    check_capacity()
    results = {}
    for platform in PLATFORMS:
        platform_name = name + "-" + platform.replace("/", "-")
        results[platform] = check_platform(
            digest, version, platform_name, platform, call=call, get=get,
            clock=clock, sleep=sleep, timeout=timeout)
    # Retain the schema 2 summary consumed by the operator release helper.
    # Its image ID is the primary customer platform (amd64); both results are
    # mandatory and independently validated in the production receipt.
    return {**results["linux/amd64"], "runner": runner, "platforms": results}


def check_platform(digest, version, name, platform, *, call, get, clock, sleep, timeout):
    volume = name + "-data"
    # Names belong to one first-attempt workflow run. Refuse reuse, even after a crash.
    if call("container", "ls", "-a", "--filter", f"name=^/{name}$", "--format", "{{.Names}}"):
        raise CheckerError("checker container already exists")
    if call("volume", "ls", "--filter", f"name=^{volume}$", "--format", "{{.Name}}"):
        raise CheckerError("checker volume already exists")
    reference = f"{IMAGE}@{digest}"
    try:
        call("volume", "create", volume)
        call("run", "--detach", "--pull=always", "--platform", platform, "--name", name,
             "--publish", "127.0.0.1::8000", "--mount", f"type=volume,source={volume},target=/data", reference)
        info = json.loads(call("inspect", name))[0]
        if info["Config"]["Image"] != reference or info["Image"] != digest:
            raise CheckerError("booted image mismatch")
        if info.get("State", {}).get("Running") is not True:
            raise CheckerError("checker container is not running")
        # With the containerd image store, .Image is the multi-platform index and
        # image inspect reports the host's variant, so ask the running container.
        machine = call("exec", name, "uname", "-m")
        if MACHINE.get(platform) != machine:
            raise CheckerError("booted platform mismatch")
        bindings = info["NetworkSettings"]["Ports"]["8000/tcp"]
        if len(bindings) != 1 or bindings[0]["HostIp"] != "127.0.0.1" or not re.fullmatch(r"[0-9]{1,5}", bindings[0]["HostPort"]):
            raise CheckerError("checker is not bound exclusively to loopback")
        base = "http://127.0.0.1:" + bindings[0]["HostPort"]
        deadline = clock() + timeout
        while True:
            try:
                evidence = smoke(base, version, get=get)
                break
            except (CheckerError, OSError, ValueError):
                if clock() >= deadline:
                    raise CheckerError("checker health/readiness/smoke timed out") from None
                sleep(5)
        # Startup success alone can hide a supervisor crash/restart loop.
        sleep(45)
        stable = json.loads(call("inspect", name))[0]
        state = stable.get("State", {})
        if (stable.get("Image") != digest or stable.get("Config", {}).get("Image") != reference
                or state.get("Running") is not True or stable.get("RestartCount") != 0
                or ("Health" in state and state["Health"].get("Status") != "healthy")):
            raise CheckerError("checker failed stability observation")
        processes = call("exec", name, "supervisorctl", "status").splitlines()
        if not processes or any(len(row.split()) < 2 or row.split()[1] != "RUNNING" for row in processes):
            raise CheckerError("checker supervisor processes are not all RUNNING")
        evidence = smoke(base, version, get=get)
        evidence.update({"digest": digest, "image_id": stable["Image"]})
    finally:
        # Also backed by the workflow's EXIT trap and always() cleanup on cancellation.
        # Cleanup failure must prevent promotion, not silently leak a fresh instance.
        try:
            call("rm", "--force", name)
        finally:
            try:
                call("volume", "rm", volume)
            finally:
                call("image", "rm", reference)
    return evidence


def validate_evidence(evidence, digest, version):
    def valid(result):
        return (isinstance(result, dict) and result.get("digest") == digest
                and result.get("image_id") == digest and result.get("version") == version
                and result.get("status") == "ok" and result.get("ready") is True
                and result.get("smoke") == {"login": True, "api_version": True, "fresh_setup": True})
    platforms = evidence.get("platforms", {})
    if (not valid(evidence) or not isinstance(evidence.get("runner"), str)
            or not evidence["runner"].strip() or set(platforms) != set(PLATFORMS)
            or not all(valid(result) for result in platforms.values())):
        raise CheckerError("invalid checker evidence for requested digest/version/platforms")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("digest", "version", "name", "runner", "output"):
        parser.add_argument("--" + key, required=True)
    args = parser.parse_args()
    result = run_checker(args.digest, args.version, args.name, args.runner)
    Path(args.output).write_text(json.dumps(result, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
