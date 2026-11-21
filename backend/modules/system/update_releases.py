"""Read only the public ODIN release list. Never persist remote text or URLs."""
import json
import re
from datetime import datetime, timezone

import httpx

from core import itar, outbound_http

RELEASES_URL = "https://api.github.com/repos/HughKantsime/runsodin/releases?per_page=20"
NOTES_URL = "https://github.com/HughKantsime/runsodin/releases/tag/v"
LIMIT = 512 * 1024
REQUEST_SECONDS = 10
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
    installed_number = version_number(installed)
    if not installed_number:
        return {"outcome": "unknown_installed_version"}
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
        if (number > installed_number and number > version_number(security_version)
                and isinstance(body, str) and "### Security" in body.splitlines()):
            security_version = version
    return {"newest": newest, "published_at": published, "security_version": security_version,
            "security": security_version is not None}


def _validate_response(response):
    if response.status_code != 200:
        raise httpx.HTTPStatusError("Expected 200", request=response.request, response=response)
    if response.headers.get("content-encoding", "identity").lower() != "identity":
        raise ValueError("Expected an unencoded response")


def fetch_releases(installed, *, transport=None):
    if itar.is_itar_mode():
        return {"outcome": "blocked"}
    if not version_number(installed):
        return {"outcome": "unknown_installed_version"}
    try:
        itar.enforce_request_destination(RELEASES_URL)
        response = outbound_http.pinned_get(
            RELEASES_URL, headers={"Accept": "application/vnd.github+json", "User-Agent": "ODIN"},
            timeout=REQUEST_SECONDS, max_response_bytes=LIMIT, validate_response=_validate_response,
            transport=transport,
        )
        return {"outcome": "ok", **parse_releases(json.loads(response.content), installed)}
    except httpx.HTTPStatusError:
        return {"outcome": "http_error"}
    except outbound_http.OutboundResponseTooLarge:
        return {"outcome": "too_large"}
    except itar.ItarOutboundBlocked:
        return {"outcome": "blocked"}
    except (TimeoutError, httpx.TimeoutException):
        return {"outcome": "timeout"}
    except httpx.HTTPError:
        return {"outcome": "unreachable"}
    except (ValueError, RecursionError):
        return {"outcome": "invalid_response"}
