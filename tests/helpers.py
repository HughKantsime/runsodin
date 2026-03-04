"""
Shared test helpers for the O.D.I.N. test suite.

Consolidates duplicate login/header helpers across test files.
"""

import os
import shutil
import subprocess
import requests


def _get_api_key():
    """Return API key from environment, or None."""
    return os.environ.get("API_KEY") or None


def login(base_url, username, password, api_key=None):
    """Login and return JWT token, or None on failure.

    Sends form-data to /api/auth/login (FastAPI OAuth2PasswordRequestForm).
    Includes X-API-Key header if provided (or from env).
    """
    if api_key is None:
        api_key = _get_api_key()
    headers = {}
    if api_key:
        headers["X-API-Key"] = api_key
    resp = requests.post(
        f"{base_url}/api/auth/login",
        data={"username": username, "password": password},
        headers=headers,
        timeout=10,
    )
    if resp.status_code == 200:
        data = resp.json()
        return data.get("access_token") or data.get("token")
    return None


def auth_headers(token):
    """Return auth headers dict with Bearer token and API key (if set)."""
    headers = {"Authorization": f"Bearer {token}"}
    api_key = _get_api_key()
    if api_key:
        headers["X-API-Key"] = api_key
    return headers


def _detect_container_runtime():
    """Detect whether we're running against Docker or k8s and return an exec helper.

    Checks ODIN_CONTAINER_EXEC env var first (e.g. "kubectl exec -n claude-code <pod> --"),
    then tries docker, then kubectl.
    """
    custom = os.environ.get("ODIN_CONTAINER_EXEC")
    if custom:
        return custom.split()

    if shutil.which("docker"):
        # Check if the odin container is running
        try:
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Running}}", "odin"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and "true" in result.stdout.lower():
                return ["docker", "exec", "odin"]
        except Exception:
            pass

    # Try kubectl — look for odin pod in common namespaces
    kubectl = shutil.which("kubectl") or os.path.expanduser("~/bin/kubectl")
    if os.path.isfile(kubectl):
        for ns in ["claude-code", "default", "odin"]:
            try:
                result = subprocess.run(
                    [kubectl, "get", "pods", "-n", ns, "-l", "app=odin-test",
                     "-o", "jsonpath={.items[0].metadata.name}"],
                    capture_output=True, text=True, timeout=10,
                )
                pod = result.stdout.strip()
                if pod:
                    return [kubectl, "exec", "-n", ns, pod, "--"]
            except Exception:
                continue

    return None


def container_exec(cmd, timeout=15):
    """Execute a command inside the O.D.I.N. container (Docker or k8s).

    Returns (returncode, stdout, stderr). Returns (1, "", "no runtime") if
    neither Docker nor kubectl is available.
    """
    runtime = _detect_container_runtime()
    if not runtime:
        return (1, "", "No container runtime found (docker/kubectl)")
    try:
        result = subprocess.run(
            runtime + cmd,
            capture_output=True, text=True, timeout=timeout,
        )
        return (result.returncode, result.stdout, result.stderr)
    except FileNotFoundError:
        return (1, "", "Container runtime binary not found")
    except subprocess.TimeoutExpired:
        return (1, "", "Command timed out")


def container_exec_python(code, timeout=15):
    """Execute a Python snippet inside the O.D.I.N. container.

    Returns (returncode, stdout, stderr).
    """
    return container_exec(["python3", "-c", code], timeout=timeout)


def restart_backend():
    """Restart the backend process to clear in-memory rate limits and lockouts.

    Works with both Docker and k8s deployments.
    """
    import time
    rc, _, _ = container_exec(["supervisorctl", "restart", "backend"], timeout=15)
    if rc == 0:
        time.sleep(8)  # Wait for backend to come back up (supervisord startsecs=5)
    return rc == 0
