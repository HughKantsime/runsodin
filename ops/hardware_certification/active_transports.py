"""Closed protocol command surfaces used only by the active worker."""

from __future__ import annotations

import time
import uuid
from typing import Callable
from urllib.parse import quote


class ActiveTransportError(RuntimeError):
    pass


def _job_id(value: int | str | None) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ActiveTransportError("PrusaLink requires an exact numeric job ID")
    return value


class BambuActiveTransport:
    def __init__(
        self, *, expected_remote_name: str,
        publisher: Callable[[dict], bool], uploader: Callable[[str], bool],
        sequence: Callable[[], str] | None = None,
    ):
        self.__remote_name = expected_remote_name
        self.__publish = publisher
        self.__upload = uploader
        self.__sequence = sequence or (lambda: str(int(time.time()) % 100000))

    def upload(self, remote_name: str) -> bool:
        return remote_name == self.__remote_name and self.__upload(remote_name)

    def start(self, remote_name: str) -> bool:
        if remote_name != self.__remote_name:
            return False
        stem = remote_name.removesuffix(".3mf")
        return self.__publish({"print": {
            "sequence_id": self.__sequence(), "command": "project_file",
            "param": "Metadata/plate_1.gcode", "subtask_name": stem,
            "url": f"ftp:///{remote_name}", "bed_type": "auto", "timelapse": False,
            "bed_leveling": True, "flow_cali": False, "vibration_cali": True,
            "layer_inspect": False, "use_ams": True, "profile_id": "0",
            "project_id": "0", "subtask_id": "0", "task_id": "0",
        }})

    def pause(self, _job_id_value=None) -> bool:
        return self.__publish({"print": {"sequence_id": "0", "command": "pause"}})

    def resume(self, _job_id_value=None) -> bool:
        return self.__publish({"print": {"sequence_id": "0", "command": "resume"}})

    def stop(self, _job_id_value=None) -> bool:
        return self.__publish({"print": {"sequence_id": "0", "command": "stop"}})


class MoonrakerActiveTransport:
    def __init__(
        self, *, expected_remote_name: str,
        poster: Callable[[str], bool], uploader: Callable[[str], bool],
    ):
        self.__remote_name = expected_remote_name
        self.__post = poster
        self.__upload = uploader

    def upload(self, remote_name: str) -> bool:
        return remote_name == self.__remote_name and self.__upload(remote_name)

    def start(self, remote_name: str) -> bool:
        return remote_name == self.__remote_name and self.__post(
            "/printer/print/start?filename=" + quote(remote_name, safe="")
        )

    def pause(self, _job_id_value=None) -> bool:
        return self.__post("/printer/print/pause")

    def resume(self, _job_id_value=None) -> bool:
        return self.__post("/printer/print/resume")

    def cancel(self, _job_id_value=None) -> bool:
        return self.__post("/printer/print/cancel")


class PrusaLinkActiveTransport:
    def __init__(
        self, *, expected_remote_name: str,
        atomic_uploader: Callable[[str, dict[str, str]], bool],
        putter: Callable[[str], bool], deleter: Callable[[str], bool],
    ):
        self.__remote_name = expected_remote_name
        self.__atomic_upload = atomic_uploader
        self.__put = putter
        self.__delete = deleter

    def upload_start(self, remote_name: str) -> bool:
        return remote_name == self.__remote_name and self.__atomic_upload(
            remote_name, {"select": "true", "print": "true"}
        )

    def pause(self, job_id_value=None) -> bool:
        return self.__put(f"/api/v1/job/{_job_id(job_id_value)}/pause")

    def resume(self, job_id_value=None) -> bool:
        return self.__put(f"/api/v1/job/{_job_id(job_id_value)}/resume")

    def stop(self, job_id_value=None) -> bool:
        return self.__delete(f"/api/v1/job/{_job_id(job_id_value)}")


class ElegooActiveTransport:
    COMMANDS = {"pause": 129, "resume": 131, "stop": 130}

    def __init__(
        self, *, mainboard_id: str, sender: Callable[[dict], bool],
        uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
        timestamp: Callable[[], int] | None = None,
    ):
        self.__mainboard_id = mainboard_id
        self.__send = sender
        self.__uuid = uuid_factory
        self.__timestamp = timestamp or (lambda: int(time.time()))

    def _command(self, name: str) -> bool:
        return self.__send({
            "Id": str(self.__uuid()),
            "Data": {
                "Cmd": self.COMMANDS[name], "Data": {},
                "RequestID": str(self.__uuid()),
                "MainboardID": self.__mainboard_id,
                "TimeStamp": self.__timestamp(), "From": 0,
            },
            "Topic": f"sdcp/request/{self.__mainboard_id}",
        })

    def pause(self, _job_id_value=None) -> bool:
        return self._command("pause")

    def resume(self, _job_id_value=None) -> bool:
        return self._command("resume")

    def stop(self, _job_id_value=None) -> bool:
        return self._command("stop")
