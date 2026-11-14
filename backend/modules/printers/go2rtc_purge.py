"""ITAR mode, go2rtc in its own container (the enterprise layout): remove from the RUNNING
go2rtc every stream that is not in the configuration ODIN has just checked.

Replacing the configuration file is not enough there. ODIN does not start or stop that
container, and go2rtc keeps the streams it loaded in memory until it is reloaded: a camera
outside the local network, saved before ITAR mode was turned on, stays reachable through
go2rtc after the file is fixed. At every start of the API container ODIN therefore asks the
running go2rtc for its streams and deletes each one whose name or source is not exactly
what was just validated. If that cannot be done, the caller refuses to start.

Every request goes to the address go2rtc_access.go2rtc_target() checked (local network
only, pinned, no proxy), follows no redirect, and carries the per-boot credentials go2rtc
may be running with (this boot's, then the previous boot's).
"""
import logging

import httpx

from modules.printers.go2rtc_access import go2rtc_restart_credential_candidates, go2rtc_target

log = logging.getLogger("odin.go2rtc")
TIMEOUT_SECONDS = 5


class Go2rtcPurgeFailed(Exception):
    """The running go2rtc could not be brought to the checked list of streams."""


def _sources(value) -> set:
    if value is None:
        return set()
    return {str(item) for item in (value if isinstance(value, (list, tuple)) else [value])}


def _unexpected(running: dict, validated: dict) -> list:
    """Names of running streams that are not exactly a validated stream."""
    names = []
    for name, state in running.items():
        producers = (state or {}).get("producers") if isinstance(state, dict) else None
        urls = {str(producer.get("url")) if isinstance(producer, dict) and producer.get("url") else ""
                for producer in (producers or [])}
        if name not in validated or not urls <= _sources(validated[name]):
            names.append(name)
    return names


def _request(client, method, url, **kwargs):
    """One request, trying each credential go2rtc may be running with. Returns the response
    (never a 401), or raises Go2rtcPurgeFailed."""
    for auth in go2rtc_restart_credential_candidates():
        response = client.request(method, url, auth=auth, **kwargs)
        if response.status_code != 401:
            return response
    raise Go2rtcPurgeFailed("go2rtc refused every ODIN credential")


def remove_streams_not_in(validated: dict) -> list:
    """Delete from the running go2rtc every stream not in `validated` ({name: source or
    list of sources}). Returns the names removed. Does nothing (returns []) when go2rtc runs
    beside the API in this container, or when nothing is listening at its address (it is not
    running, so it holds no stream). Raises Go2rtcPurgeFailed for anything else that leaves
    the result unknown: a refused address, a timeout, an error answer, a stream that is
    still there afterwards."""
    try:
        go2rtc = go2rtc_target()  # refused under ITAR mode unless it is on the local network
    except Exception as exc:
        raise Go2rtcPurgeFailed("the go2rtc address is not usable") from exc
    if not go2rtc.remote:
        return []
    settings = {**go2rtc.client_kwargs, "trust_env": False, "timeout": TIMEOUT_SECONDS, "follow_redirects": False}
    settings.pop("auth", None)
    removed = []
    try:
        with httpx.Client(**settings) as client:
            try:
                listing = _request(client, "GET", f"{go2rtc.url}/api/streams")
            except httpx.ConnectError:
                log.info("go2rtc is not running at its configured address; it holds no stream")
                return []
            if listing.status_code != 200:
                raise Go2rtcPurgeFailed(f"go2rtc answered {listing.status_code} to the stream list")
            running = listing.json()
            if not isinstance(running, dict):
                raise Go2rtcPurgeFailed("go2rtc's stream list is not readable")
            for name in _unexpected(running, validated):
                answer = _request(client, "DELETE", f"{go2rtc.url}/api/streams", params={"src": name})
                if answer.status_code not in (200, 204, 404):
                    raise Go2rtcPurgeFailed(f"go2rtc answered {answer.status_code} when a stream was removed")
                removed.append(name)
            if removed:
                after = _request(client, "GET", f"{go2rtc.url}/api/streams")
                left = _unexpected(after.json(), validated) if after.status_code == 200 else removed
                if left:
                    raise Go2rtcPurgeFailed(f"{len(left)} stream(s) are still in go2rtc after removal")
    except Go2rtcPurgeFailed:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise Go2rtcPurgeFailed(f"go2rtc could not be reached: {type(exc).__name__}") from exc
    return removed
