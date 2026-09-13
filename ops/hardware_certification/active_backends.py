"""Protocol-specific ActiveBackend adapters with no generic dispatch surface."""

from __future__ import annotations

from typing import Callable

from .active import Observation
from .active_transports import (
    BambuActiveTransport, ElegooActiveTransport, MoonrakerActiveTransport,
    PrusaLinkActiveTransport,
)


class _Observed:
    def __init__(self, observer: Callable[[], Observation], closer: Callable[[], None] = lambda: None):
        self.__observer = observer
        self.__closer = closer

    def observe(self) -> Observation:
        return self.__observer()

    def close(self) -> None:
        self.__closer()


class BambuExerciseBackend(_Observed):
    def __init__(self, observer: Callable[[], Observation], transport: BambuActiveTransport, closer: Callable[[], None] = lambda: None):
        super().__init__(observer, closer); self.__transport = transport

    def upload(self, remote_name): return self.__transport.upload(remote_name)
    def start(self, remote_name): return self.__transport.start(remote_name)
    def pause(self, job_id): return self.__transport.pause(job_id)
    def resume(self, job_id): return self.__transport.resume(job_id)
    def stop(self, job_id): return self.__transport.stop(job_id)
    def read_ams(self): return self.observe().ams_slots


class MoonrakerExerciseBackend(_Observed):
    def __init__(self, observer: Callable[[], Observation], transport: MoonrakerActiveTransport, closer: Callable[[], None] = lambda: None):
        super().__init__(observer, closer); self.__transport = transport

    def upload(self, remote_name): return self.__transport.upload(remote_name)
    def start(self, remote_name): return self.__transport.start(remote_name)
    def pause(self, job_id): return self.__transport.pause(job_id)
    def resume(self, job_id): return self.__transport.resume(job_id)
    def cancel(self, job_id): return self.__transport.cancel(job_id)


class PrusaLinkExerciseBackend(_Observed):
    def __init__(self, observer: Callable[[], Observation], transport: PrusaLinkActiveTransport, closer: Callable[[], None] = lambda: None):
        super().__init__(observer, closer); self.__transport = transport

    def upload_start(self, remote_name): return self.__transport.upload_start(remote_name)
    def pause(self, job_id): return self.__transport.pause(job_id)
    def resume(self, job_id): return self.__transport.resume(job_id)
    def stop(self, job_id): return self.__transport.stop(job_id)


class ElegooExerciseBackend(_Observed):
    def __init__(self, observer: Callable[[], Observation], transport: ElegooActiveTransport, closer: Callable[[], None] = lambda: None):
        super().__init__(observer, closer); self.__transport = transport

    def pause(self, job_id): return self.__transport.pause(job_id)
    def resume(self, job_id): return self.__transport.resume(job_id)
    def stop(self, job_id): return self.__transport.stop(job_id)
