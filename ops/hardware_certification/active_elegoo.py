"""Peer-pinned Elegoo SDCP active session."""

from __future__ import annotations

import json
import socket

import websocket

from .active import ExerciseError, Observation
from .active_backends import ElegooExerciseBackend
from .active_transports import ElegooActiveTransport
from .config import ResolvedTarget
from .passive.parsers import ParseError, parse_elegoo_sample
from .passive.transports import PassivePolicyError, decode_bounded_json_object


class ElegooLiveSession:
    def __init__(self, target: ResolvedTarget):
        mainboard_id = target.connection.get("mainboard_id")
        if not isinstance(mainboard_id, str) or not mainboard_id:
            raise ExerciseError("Elegoo active exercise requires a configured mainboard ID")
        self.mainboard_id = mainboard_id
        raw_socket = socket.create_connection(
            (target.address, target.connection["port"]), timeout=15,
        )
        try:
            self.__socket = websocket.create_connection(
                f"ws://{target.address}:{target.connection['port']}/websocket",  # nosemgrep: javascript.lang.security.detect-insecure-websocket.detect-insecure-websocket -- Elegoo SDCP is LAN-only ws:// and the resolved private peer is pinned
                timeout=15, host=target.connection["host"], suppress_origin=True,
                socket=raw_socket, http_proxy_host=None, http_no_proxy=[target.address],
            )
        except Exception:
            raw_socket.close()
            raise

    def observe(self) -> Observation:
        try:
            frame = self.__socket.recv()
            if not isinstance(frame, (str, bytes)) or len(frame) > 262_144:
                raise ExerciseError("Elegoo status frame size is invalid")
            payload = decode_bounded_json_object(frame)
            sample = parse_elegoo_sample(payload)
        except (ParseError, PassivePolicyError, ValueError, OSError) as exc:
            raise ExerciseError("Elegoo status observation failed") from exc
        return Observation(sample.state, sample.filename, sample.job_id)

    def __send_closed(self, frame: dict) -> bool:
        if frame.get("Topic") != f"sdcp/request/{self.mainboard_id}":
            raise ExerciseError("Elegoo command topic changed")
        self.__socket.send(json.dumps(frame, separators=(",", ":")))
        return True

    def close(self) -> None:
        self.__socket.close()

    def backend(self) -> ElegooExerciseBackend:
        transport = ElegooActiveTransport(mainboard_id=self.mainboard_id, sender=self.__send_closed)
        return ElegooExerciseBackend(self.observe, transport, self.close)
