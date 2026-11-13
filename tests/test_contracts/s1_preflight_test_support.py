"""Explicit passing preflight for legacy S1 scenarios; no device or broker access."""
from urllib.error import HTTPError
from modules.printers.kobra_s1 import DECOY_FILENAME


def passing_preflight(monkeypatch):
    from modules.printers.adapters import moonraker, anycubic_lan
    identity = moonraker.MoonrakerPrinter.device_file_identity
    def missing(self, name):
        if name == DECOY_FILENAME:
            raise HTTPError('http://fixture.invalid', 404, 'Missing', {}, None)
        return identity(self, name)
    def send(self, payload, *, before_publish):
        before_publish()
        return dict(processed=True)
    monkeypatch.setattr(moonraker.MoonrakerPrinter, 'device_file_identity', missing)
    monkeypatch.setattr(anycubic_lan.AnycubicLANPrinter, 'send_decoy', send)
