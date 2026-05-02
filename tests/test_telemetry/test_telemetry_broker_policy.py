"""Unit tests for the Bambu broker policy resolver (ODIN-142 Wake 2b).

Covers the gating matrix:

  | global gate | host in allowlist | ITAR | expected           |
  | ----------- | ----------------- | ---- | ------------------ |
  | unset       | n/a               | off  | TLS:8883 (default) |
  | set         | no                | off  | TLS:8883 (fail-closed by host) |
  | set         | yes               | on   | TLS:8883 (ITAR overrides) |
  | set         | yes               | off  | plain:1883 (bypass) |

Plus boot-audit failure modes:
  * ITAR + bypass configured        → RuntimeError
  * Invalid port string             → RuntimeError
  * Allowlist contains public IP    → RuntimeError
"""
from __future__ import annotations

import logging
import os

import pytest

from backend.modules.printers.telemetry.bambu import broker_policy as bp


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Strip every env var the policy resolver inspects, so each test
    can declare its own ground truth without inheriting another test's
    mutations."""
    for name in (
        "ODIN_ALLOW_INSECURE_BAMBU_BROKER",
        "ODIN_BAMBU_INSECURE_BROKER_HOSTS",
        "ODIN_BAMBU_INSECURE_BROKER_PORT",
        "ODIN_ITAR_MODE",
    ):
        monkeypatch.delenv(name, raising=False)
    yield


def test_default_returns_tls_8883_for_real_printer():
    ep = bp.resolve_bambu_broker_config("192.168.1.42", printer_id="p-1")
    assert ep.host == "192.168.1.42"
    assert ep.port == 8883
    assert ep.use_tls is True


def test_default_returns_tls_for_mosquitto_when_no_env(monkeypatch):
    """Even matching the canonical demo host returns TLS unless gates set."""
    ep = bp.resolve_bambu_broker_config("mosquitto", printer_id="p-1")
    assert ep.use_tls is True
    assert ep.port == 8883


def test_global_gate_alone_does_not_unlock(monkeypatch):
    monkeypatch.setenv("ODIN_ALLOW_INSECURE_BAMBU_BROKER", "1")
    ep = bp.resolve_bambu_broker_config("mosquitto", printer_id="p-1")
    assert ep.use_tls is True
    assert ep.port == 8883


def test_allowlist_alone_does_not_unlock(monkeypatch):
    monkeypatch.setenv("ODIN_BAMBU_INSECURE_BROKER_HOSTS", "mosquitto")
    ep = bp.resolve_bambu_broker_config("mosquitto", printer_id="p-1")
    assert ep.use_tls is True
    assert ep.port == 8883


def test_both_gates_set_with_matching_host_unlocks_plaintext(monkeypatch, caplog):
    monkeypatch.setenv("ODIN_ALLOW_INSECURE_BAMBU_BROKER", "1")
    monkeypatch.setenv("ODIN_BAMBU_INSECURE_BROKER_HOSTS", "mosquitto,127.0.0.1,localhost")
    with caplog.at_level(logging.WARNING, logger="odin.bambu.broker_policy"):
        ep = bp.resolve_bambu_broker_config("mosquitto", printer_id="p-1")
    assert ep.host == "mosquitto"
    assert ep.port == 1883
    assert ep.use_tls is False
    assert any("bambu_broker_insecure_bypass" in r.message for r in caplog.records)


def test_bypass_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("ODIN_ALLOW_INSECURE_BAMBU_BROKER", "1")
    monkeypatch.setenv("ODIN_BAMBU_INSECURE_BROKER_HOSTS", "MosQuitto")
    ep = bp.resolve_bambu_broker_config("mosquitto", printer_id="p-1")
    assert ep.use_tls is False
    assert ep.port == 1883


def test_non_allowlisted_host_falls_back_to_tls(monkeypatch):
    monkeypatch.setenv("ODIN_ALLOW_INSECURE_BAMBU_BROKER", "1")
    monkeypatch.setenv("ODIN_BAMBU_INSECURE_BROKER_HOSTS", "mosquitto")
    ep = bp.resolve_bambu_broker_config("192.168.1.42", printer_id="p-1")
    assert ep.use_tls is True
    assert ep.port == 8883


def test_itar_mode_blocks_bypass_at_runtime(monkeypatch):
    monkeypatch.setenv("ODIN_ALLOW_INSECURE_BAMBU_BROKER", "1")
    monkeypatch.setenv("ODIN_BAMBU_INSECURE_BROKER_HOSTS", "mosquitto")
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    ep = bp.resolve_bambu_broker_config("mosquitto", printer_id="p-1")
    assert ep.use_tls is True
    assert ep.port == 8883


def test_custom_insecure_port_honored(monkeypatch):
    monkeypatch.setenv("ODIN_ALLOW_INSECURE_BAMBU_BROKER", "1")
    monkeypatch.setenv("ODIN_BAMBU_INSECURE_BROKER_HOSTS", "mosquitto")
    monkeypatch.setenv("ODIN_BAMBU_INSECURE_BROKER_PORT", "11883")
    ep = bp.resolve_bambu_broker_config("mosquitto", printer_id="p-1")
    assert ep.port == 11883
    assert ep.use_tls is False


def test_invalid_port_at_runtime_falls_back_to_tls(monkeypatch):
    monkeypatch.setenv("ODIN_ALLOW_INSECURE_BAMBU_BROKER", "1")
    monkeypatch.setenv("ODIN_BAMBU_INSECURE_BROKER_HOSTS", "mosquitto")
    monkeypatch.setenv("ODIN_BAMBU_INSECURE_BROKER_PORT", "banana")
    ep = bp.resolve_bambu_broker_config("mosquitto", printer_id="p-1")
    assert ep.use_tls is True
    assert ep.port == 8883


def test_empty_host_returns_tls_default(monkeypatch):
    monkeypatch.setenv("ODIN_ALLOW_INSECURE_BAMBU_BROKER", "1")
    monkeypatch.setenv("ODIN_BAMBU_INSECURE_BROKER_HOSTS", "mosquitto")
    ep = bp.resolve_bambu_broker_config("", printer_id="p-1")
    assert ep.use_tls is True


# ---------- boot audit ----------


def test_boot_audit_clean_env_passes(monkeypatch):
    bp.enforce_boot_audit()  # no env set; should be a no-op


def test_boot_audit_clean_bypass_passes(monkeypatch, caplog):
    monkeypatch.setenv("ODIN_ALLOW_INSECURE_BAMBU_BROKER", "1")
    monkeypatch.setenv("ODIN_BAMBU_INSECURE_BROKER_HOSTS", "mosquitto,localhost")
    with caplog.at_level(logging.WARNING, logger="odin.bambu.broker_policy"):
        bp.enforce_boot_audit()
    assert any("bambu_broker_insecure_bypass_enabled" in r.message for r in caplog.records)


def test_boot_audit_itar_plus_bypass_fails(monkeypatch):
    monkeypatch.setenv("ODIN_ALLOW_INSECURE_BAMBU_BROKER", "1")
    monkeypatch.setenv("ODIN_BAMBU_INSECURE_BROKER_HOSTS", "mosquitto")
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    with pytest.raises(RuntimeError, match="ODIN_ITAR_MODE=1"):
        bp.enforce_boot_audit()


def test_boot_audit_invalid_port_fails(monkeypatch):
    monkeypatch.setenv("ODIN_ALLOW_INSECURE_BAMBU_BROKER", "1")
    monkeypatch.setenv("ODIN_BAMBU_INSECURE_BROKER_HOSTS", "mosquitto")
    monkeypatch.setenv("ODIN_BAMBU_INSECURE_BROKER_PORT", "banana")
    with pytest.raises(RuntimeError, match="must be an integer"):
        bp.enforce_boot_audit()


def test_boot_audit_out_of_range_port_fails(monkeypatch):
    monkeypatch.setenv("ODIN_ALLOW_INSECURE_BAMBU_BROKER", "1")
    monkeypatch.setenv("ODIN_BAMBU_INSECURE_BROKER_HOSTS", "mosquitto")
    monkeypatch.setenv("ODIN_BAMBU_INSECURE_BROKER_PORT", "70000")
    with pytest.raises(RuntimeError, match="out of range"):
        bp.enforce_boot_audit()


def test_boot_audit_public_ip_in_allowlist_fails(monkeypatch):
    monkeypatch.setenv("ODIN_ALLOW_INSECURE_BAMBU_BROKER", "1")
    monkeypatch.setenv("ODIN_BAMBU_INSECURE_BROKER_HOSTS", "8.8.8.8")
    with pytest.raises(RuntimeError, match="public IP"):
        bp.enforce_boot_audit()


def test_boot_audit_private_ip_in_allowlist_passes(monkeypatch):
    monkeypatch.setenv("ODIN_ALLOW_INSECURE_BAMBU_BROKER", "1")
    monkeypatch.setenv("ODIN_BAMBU_INSECURE_BROKER_HOSTS", "127.0.0.1,10.0.0.5")
    bp.enforce_boot_audit()


def test_boot_audit_hostname_in_allowlist_passes(monkeypatch):
    monkeypatch.setenv("ODIN_ALLOW_INSECURE_BAMBU_BROKER", "1")
    monkeypatch.setenv("ODIN_BAMBU_INSECURE_BROKER_HOSTS", "mosquitto,kube-svc.local")
    bp.enforce_boot_audit()


def test_is_bypass_configured_false_by_default():
    assert bp.is_bypass_configured() is False


def test_is_bypass_configured_requires_both_gates(monkeypatch):
    monkeypatch.setenv("ODIN_ALLOW_INSECURE_BAMBU_BROKER", "1")
    assert bp.is_bypass_configured() is False  # allowlist still empty
    monkeypatch.setenv("ODIN_BAMBU_INSECURE_BROKER_HOSTS", "mosquitto")
    assert bp.is_bypass_configured() is True
