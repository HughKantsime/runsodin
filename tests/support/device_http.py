"""TEST ONLY: route device HTTP (core.webhook_utils.device_request) to fake verbs.

Device requests use a fresh requests.Session (no env proxies, no redirects).
Tests that model a device with plain verb functions route Session.request to
them; the redirect policy is asserted, then removed from the forwarded kwargs.
"""
import requests


def route_device_requests(monkeypatch, **verbs):
    def request(self, method, url, **kwargs):
        assert kwargs.pop("allow_redirects") is False, "device requests must not follow redirects"
        assert self.trust_env is False and self.proxies == {}, "device requests must not use env proxies"
        return verbs[method.lower()](url, **kwargs)
    monkeypatch.setattr(requests.Session, "request", request)
