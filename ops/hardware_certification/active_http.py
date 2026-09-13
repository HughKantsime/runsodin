"""Peer-pinned Moonraker and PrusaLink active sessions."""

from __future__ import annotations

import http.client
import re
import socket
import ssl

import requests
from requests.auth import HTTPDigestAuth

from .active import ExerciseError, Observation
from .active_backends import MoonrakerExerciseBackend, PrusaLinkExerciseBackend
from .active_transports import MoonrakerActiveTransport, PrusaLinkActiveTransport
from .assets import ValidatedAsset
from .config import ResolvedTarget
from .passive.observers import MOON_QUERY
from .passive.parsers import ParseError, parse_moonraker_sample, parse_prusalink_sample
from .passive.transports import PinnedDigestHttpTransport, PinnedReadOnlyHttpTransport


MOON_POSTS = frozenset({
    "/printer/print/pause", "/printer/print/resume", "/printer/print/cancel",
})
PRUSA_PUT = re.compile(r"^/api/v1/job/[0-9]+/(pause|resume)$")
PRUSA_DELETE = re.compile(r"^/api/v1/job/[0-9]+$")


def _active_pinned_connection(target: ResolvedTarget, timeout: float):
    """Build an active-only HTTP connection pinned to the validated peer."""
    use_tls = bool(target.connection.get("tls", False))
    connection_type = http.client.HTTPSConnection if use_tls else http.client.HTTPConnection
    kwargs = {"timeout": timeout}
    if use_tls:
        kwargs["context"] = ssl.create_default_context()
    connection = connection_type(
        target.connection["host"], target.connection["port"], **kwargs,
    )
    connection._create_connection = lambda _target, timeout=None, source_address=None: socket.create_connection(  # type: ignore[attr-defined]
        (target.address, target.connection["port"]),
        timeout if timeout is not None else kwargs["timeout"], source_address,
    )
    return connection


class _PinnedHttpMutator:
    """Private generic primitive; public session methods enforce exact paths."""

    def __init__(self, target: ResolvedTarget):
        self.target = target
        address = f"[{target.address}]" if ":" in target.address else target.address
        self.base_url = f"http://{address}:{target.connection['port']}"

    def _headers(self) -> dict[str, str]:
        headers = {
            "Host": self.target.connection["host"], "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        if self.target.connection.get("api_key"):
            headers["X-Api-Key"] = self.target.connection["api_key"]
        return headers

    def _auth(self):
        connection = self.target.connection
        if "api_key" in connection or self.target.protocol != "prusalink":
            return None
        return HTTPDigestAuth(connection["username"], connection["password"])

    def _request(self, method: str, path: str, **kwargs) -> bool:
        if self.target.connection.get("tls"):
            if kwargs:
                raise ExerciseError("TLS uploads require the bounded streaming path")
            connection = _active_pinned_connection(self.target, 15.0)
            try:
                connection.request(method, path, headers=self._headers())
                response = connection.getresponse()
                response.read(65_536)
                return 200 <= response.status < 300
            finally:
                connection.close()
        with requests.Session() as session:
            session.trust_env = False
            response = session.request(
                method, self.base_url + path, headers=self._headers(), auth=self._auth(),
                timeout=kwargs.pop("timeout", 15), allow_redirects=False, **kwargs,
            )
            try:
                return 200 <= response.status_code < 300 and not response.is_redirect
            finally:
                response.close()

    def _tls_multipart(self, path: str, asset: ValidatedAsset, remote_name: str, fields: dict[str, str]) -> bool:
        boundary = "ODINCertBoundary7f6a3d2c"
        field_parts = "".join(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
            for name, value in fields.items()
        ).encode()
        prefix = field_parts + (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{remote_name}"\r\n'
            'Content-Type: application/octet-stream\r\n\r\n'
        ).encode()
        suffix = f"\r\n--{boundary}--\r\n".encode()
        connection = _active_pinned_connection(self.target, 120.0)
        try:
            connection.putrequest("POST", path, skip_host=True, skip_accept_encoding=True)
            for name, value in self._headers().items():
                connection.putheader(name, value)
            connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
            connection.putheader("Content-Length", str(len(prefix) + asset.size + len(suffix)))
            connection.endheaders()
            connection.send(prefix)
            with asset.open() as stream:
                while chunk := stream.read(64 * 1024):
                    connection.send(chunk)
            connection.send(suffix)
            response = connection.getresponse()
            response.read(65_536)
            return 200 <= response.status < 300
        finally:
            connection.close()

    def moon_post(self, path: str, remote_name: str) -> bool:
        if path not in MOON_POSTS and path != f"/printer/print/start?filename={remote_name}":
            raise ExerciseError("Moonraker active path changed")
        return self._request("POST", path)

    def moon_upload(self, asset: ValidatedAsset, remote_name: str) -> bool:
        if self.target.connection.get("tls"):
            return self._tls_multipart("/server/files/upload", asset, remote_name, {"root": "gcodes"})
        with asset.open() as stream:
            return self._request(
                "POST", "/server/files/upload", timeout=120,
                files={"file": (remote_name, stream, "application/octet-stream")},
                data={"root": "gcodes"},
            )

    def prusa_put(self, path: str) -> bool:
        if not PRUSA_PUT.fullmatch(path):
            raise ExerciseError("PrusaLink PUT path changed")
        return self._request("PUT", path)

    def prusa_delete(self, path: str) -> bool:
        if not PRUSA_DELETE.fullmatch(path):
            raise ExerciseError("PrusaLink DELETE path changed")
        return self._request("DELETE", path)

    def prusa_upload_start(self, asset: ValidatedAsset, remote_name: str, fields: dict[str, str]) -> bool:
        if fields != {"select": "true", "print": "true"}:
            raise ExerciseError("PrusaLink upload-start fields changed")
        if self.target.connection.get("tls"):
            return self._tls_multipart("/api/files/local", asset, remote_name, fields)
        with asset.open() as stream:
            return self._request(
                "POST", "/api/files/local", timeout=120,
                files={"file": (remote_name, stream, "application/octet-stream")},
                data=fields,
            )


class MoonrakerLiveSession:
    def __init__(self, target: ResolvedTarget, remote_name: str, asset_path: ValidatedAsset | None):
        self.target = target
        self.remote_name = remote_name
        self.asset = asset_path
        self.reader = PinnedReadOnlyHttpTransport(
            host=target.connection["host"], address=target.address,
            port=target.connection["port"], protocol="moonraker",
            use_tls=bool(target.connection.get("tls", False)), timeout=15.0,
        )
        self.mutator = _PinnedHttpMutator(target)

    def _headers(self):
        key = self.target.connection.get("api_key")
        return {"X-Api-Key": key} if key else None

    def observe(self) -> Observation:
        try:
            sample = parse_moonraker_sample(self.reader.get(MOON_QUERY, self._headers()))
        except (ParseError, OSError, ValueError) as exc:
            raise ExerciseError("Moonraker status observation failed") from exc
        return Observation(sample.state, sample.filename, sample.job_id)

    def upload(self, remote_name: str) -> bool:
        return bool(self.asset and remote_name == self.remote_name and self.mutator.moon_upload(self.asset, remote_name))

    def close(self) -> None:
        return None

    def backend(self) -> MoonrakerExerciseBackend:
        transport = MoonrakerActiveTransport(
            expected_remote_name=self.remote_name,
            poster=lambda path: self.mutator.moon_post(path, self.remote_name),
            uploader=self.upload,
        )
        return MoonrakerExerciseBackend(self.observe, transport, self.close)


class PrusaLinkLiveSession:
    def __init__(self, target: ResolvedTarget, remote_name: str, asset_path: ValidatedAsset | None):
        self.target = target
        self.remote_name = remote_name
        self.asset = asset_path
        connection = target.connection
        if "api_key" in connection:
            self.reader = PinnedReadOnlyHttpTransport(
                host=connection["host"], address=target.address, port=connection["port"],
                protocol="prusalink", use_tls=bool(connection.get("tls", False)), timeout=15.0,
            )
            self.headers = {"X-Api-Key": connection["api_key"]}
        else:
            self.reader = PinnedDigestHttpTransport(
                host=connection["host"], address=target.address, port=connection["port"],
                username=connection["username"], password=connection["password"], timeout=15.0,
            )
            self.headers = None
        self.mutator = _PinnedHttpMutator(target)

    def observe(self) -> Observation:
        try:
            sample = parse_prusalink_sample(
                self.reader.get("/api/v1/status", self.headers),
                self.reader.get("/api/job", self.headers),
            )
        except (ParseError, OSError, ValueError) as exc:
            raise ExerciseError("PrusaLink status observation failed") from exc
        return Observation(sample.state, sample.filename, sample.job_id)

    def atomic_upload(self, remote_name: str, fields: dict[str, str]) -> bool:
        return bool(
            self.asset and remote_name == self.remote_name
            and self.mutator.prusa_upload_start(self.asset, remote_name, fields)
        )

    def close(self) -> None:
        return None

    def backend(self) -> PrusaLinkExerciseBackend:
        transport = PrusaLinkActiveTransport(
            expected_remote_name=self.remote_name, atomic_uploader=self.atomic_upload,
            putter=self.mutator.prusa_put, deleter=self.mutator.prusa_delete,
        )
        return PrusaLinkExerciseBackend(self.observe, transport, self.close)
