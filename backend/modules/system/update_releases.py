"""Read only the public ODIN release list. Never persist remote text or URLs."""
import json
import re
import time
from datetime import datetime, timezone

import httpx

from core import itar

RELEASES_URL = "https://api.github.com/repos/HughKantsime/runsodin/releases?per_page=20"
NOTES_URL = "https://github.com/HughKantsime/runsodin/releases/tag/v"
LIMIT = 512 * 1024
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


def version_number(version):
    if isinstance(version, str) and len(version) <= 64 and VERSION.fullmatch(version):
        return tuple(map(int, version.split(".")))
    return ()


def published_date(value):
    if isinstance(value, str):
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if stamp.tzinfo is not None:
                return stamp.astimezone(timezone.utc).isoformat()
        except (ValueError, OverflowError):
            pass
    return None


def parse_releases(data, installed):
    if not isinstance(data, list):
        raise ValueError("Expected release list")
    newest = security_version = published = None
    for release in data:
        if not isinstance(release, dict) or release.get("draft") is not False or release.get("prerelease") is not False:
            continue
        tag = release.get("tag_name")
        if not isinstance(tag, str) or not tag.startswith("v"):
            continue
        version = tag[1:]
        number = version_number(version)
        if number > version_number(newest):
            newest, published = version, published_date(release.get("published_at"))
        body = release.get("body")
        if (number > version_number(installed) and number > version_number(security_version)
                and isinstance(body, str) and "### Security" in body.splitlines()):
            security_version = version
    return {"newest": newest, "published_at": published, "security_version": security_version,
            "security": security_version is not None}


def fetch_releases(installed, *, transport=None):
    if itar.is_itar_mode():
        return {"outcome": "blocked"}
    try:
        itar.enforce_request_destination(RELEASES_URL)
        deadline = time.monotonic() + 10
        with itar.pin_for_request(RELEASES_URL), httpx.Client(
            trust_env=itar.should_trust_env(), timeout=10, verify=True, follow_redirects=False, transport=transport,
        ) as client:
            # A standalone request omits httpx's default encoding and connection headers.
            request = httpx.Request("GET", RELEASES_URL, headers={"Accept": "application/vnd.github+json", "User-Agent": "ODIN"})
            response = client.send(request, stream=True)
            try:
                if response.status_code != 200:
                    return {"outcome": "http_error"}
                # We requested no encoding. Reject compressed data before any decompression.
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    return {"outcome": "invalid_response"}
                body = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() >= deadline:
                        return {"outcome": "timeout"}
                    if len(chunk) + len(body) > LIMIT:
                        return {"outcome": "too_large"}
                    body.extend(chunk)
                return {"outcome": "ok", **parse_releases(json.loads(body), installed)}
            finally:
                response.close()
    except itar.ItarOutboundBlocked:
        return {"outcome": "blocked"}
    except httpx.TimeoutException:
        return {"outcome": "timeout"}
    except httpx.HTTPError:
        return {"outcome": "unreachable"}
    except (ValueError, RecursionError):
        return {"outcome": "invalid_response"}
