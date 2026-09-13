from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ops.edu_sandbox.errors import SecretInputError, StateError, ValidationError
from ops.edu_sandbox.identity import (
    assert_path_owned,
    compose_project,
    sandbox_directory,
    validate_sandbox_id,
    validate_state_root,
)
from ops.edu_sandbox.secrets import (
    LICENSE_LIMIT,
    REQUEST_LICENSE_LIMIT,
    bytes_stream,
    read_bounded_stdin,
    read_json_object,
    sanitized_text,
    write_secret,
)
from ops.edu_sandbox.state import LifecycleState, Phase, load_state, save_state


@pytest.mark.parametrize("value", ["abc", "school-01", "a" * 48, "0-0"])
def test_sandbox_ids_accept_bounded_slug(value: str):
    assert validate_sandbox_id(value) == value
    assert compose_project(value) == f"odin-edu-{value}"


@pytest.mark.parametrize(
    "value",
    ["", "ab", "A-school", "-school", "school_1", "school/one", "a" * 49],
)
def test_sandbox_ids_reject_unsafe_values(value: str):
    with pytest.raises(ValidationError):
        validate_sandbox_id(value)


def test_state_root_requires_absolute_non_root(tmp_path: Path):
    assert validate_state_root(tmp_path) == tmp_path.resolve()
    with pytest.raises(ValidationError):
        validate_state_root(Path("relative"))
    with pytest.raises(ValidationError):
        validate_state_root(Path("/"))


def test_sandbox_directory_and_owned_path_reject_escape(tmp_path: Path):
    directory = sandbox_directory(tmp_path, "school-01")
    assert directory == tmp_path / "school-01"
    assert assert_path_owned(directory / "state.json", directory) == directory / "state.json"
    with pytest.raises(ValidationError):
        assert_path_owned(tmp_path / "foreign", directory)


def test_owned_path_rejects_symlink_escape(tmp_path: Path):
    directory = tmp_path / "school-01"
    outside = tmp_path / "outside"
    directory.mkdir()
    outside.mkdir()
    (directory / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValidationError):
        assert_path_owned(directory / "link" / "secret", directory)


def test_state_round_trip_is_atomic_and_mode_restricted(tmp_path: Path):
    directory = sandbox_directory(tmp_path, "school-01")
    path = directory / "state.json"
    state = LifecycleState("school-01", Phase.PREPARING)
    state.transition(Phase.PREPARED, detail="candidate stopped with identity intact")
    save_state(path, state, directory)

    loaded = load_state(path)
    assert loaded == state
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(directory).st_mode & 0o777 == 0o700
    assert not list(directory.glob(".state-*"))


def test_state_rejects_unknown_fields(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"sandbox_id": "school-01", "phase": "PREPARED", "secret": "no"}))
    with pytest.raises(ValidationError, match="unexpected fields"):
        load_state(path)


def test_legal_transition_sequence_and_terminal_purge():
    state = LifecycleState("school-01", Phase.PREPARING)
    state.transition(Phase.PREPARED, detail="prepared")
    state.transition(Phase.REQUESTING_LICENSE, detail="request")
    state.transition(Phase.PREPARED, detail="request written")
    state.transition(Phase.ACTIVATING, detail="validate")
    state.transition(Phase.ACTIVE, detail="ready")
    state.transition(Phase.RESETTING, detail="reset")
    state.transition(Phase.ACTIVE, detail="reset complete")
    state.transition(Phase.EXPIRED, detail="lease expired")
    state.transition(Phase.PURGING, detail="purge")
    state.transition(Phase.PURGED, detail="absence proven")
    with pytest.raises(StateError):
        state.transition(Phase.PREPARING, detail="cannot resurrect")


def test_illegal_prepared_to_expired_transition_is_rejected():
    state = LifecycleState("school-01", Phase.PREPARED)
    with pytest.raises(StateError, match="PREPARED -> EXPIRED"):
        state.transition(Phase.EXPIRED, detail="not activated")


def test_bounded_stdin_rejects_tty_empty_and_oversize():
    with pytest.raises(SecretInputError, match="non-TTY"):
        read_bounded_stdin(bytes_stream(b"value"), limit=10, is_tty=True)
    with pytest.raises(SecretInputError, match="empty"):
        read_bounded_stdin(bytes_stream(b"  \n"), limit=10, is_tty=False)
    with pytest.raises(SecretInputError, match="exceeds"):
        read_bounded_stdin(bytes_stream(b"x" * 11), limit=10, is_tty=False)
    assert read_bounded_stdin(bytes_stream(b"x" * 10), limit=10, is_tty=False) == b"x" * 10


def test_secret_json_limits_are_fixed_and_object_only():
    assert REQUEST_LICENSE_LIMIT == 64 * 1024
    assert LICENSE_LIMIT == 1024 * 1024
    assert read_json_object(
        bytes_stream(b'{"key":"K","nonce":"N"}'),
        limit=REQUEST_LICENSE_LIMIT,
        is_tty=False,
    ) == {"key": "K", "nonce": "N"}
    with pytest.raises(SecretInputError, match="JSON object"):
        read_json_object(bytes_stream(b"[]"), limit=REQUEST_LICENSE_LIMIT, is_tty=False)


def test_secret_file_is_mode_0600_and_digest_only(tmp_path: Path):
    directory = sandbox_directory(tmp_path, "school-01")
    content = b"sensitive-license-material"
    path = directory / "secrets" / "license.json"
    digest = write_secret(path, content, directory)
    assert len(digest) == 64
    assert path.read_bytes() == content
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(path.parent).st_mode & 0o777 == 0o700


def test_secret_write_rejects_in_sandbox_symlink_without_leaking(tmp_path: Path):
    directory = sandbox_directory(tmp_path, "school-01")
    public = directory / "public"
    secrets = directory / "secrets"
    public.mkdir(parents=True)
    secrets.mkdir()
    public_copy = public / "license-leak.json"
    public_copy.write_bytes(b"unchanged-public-content")
    destination = secrets / "license.json"
    destination.symlink_to(public_copy)

    with pytest.raises(SecretInputError, match="symlink"):
        write_secret(destination, b"sensitive-license-material", directory)

    assert public_copy.read_bytes() == b"unchanged-public-content"
    assert destination.is_symlink()
    assert not list(secrets.glob(".secret-*"))


def test_secret_write_rejects_symlinked_parent_inside_sandbox(tmp_path: Path):
    directory = sandbox_directory(tmp_path, "school-01")
    actual = directory / "actual-secrets"
    directory.mkdir(parents=True)
    actual.mkdir()
    (directory / "secrets").symlink_to(actual, target_is_directory=True)

    with pytest.raises(SecretInputError, match="symlink"):
        write_secret(directory / "secrets" / "license.json", b"secret", directory)

    assert not (actual / "license.json").exists()


def test_retained_text_redacts_known_values_and_rejects_patterns():
    secret = "long-sensitive-value"
    assert sanitized_text(f"result={secret}", [secret]) == "result=[REDACTED]"
    assert sanitized_text("JWT_SECRET_KEY=short", []) == "[REDACTED]"
