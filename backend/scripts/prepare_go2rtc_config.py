"""Container start (docker/entrypoint.sh), before supervisord: rotate the per-boot camera
relay secret (which also derives go2rtc's RTSP credentials) and write the go2rtc config,
so go2rtc starts with this boot's configuration instead of being restarted by the API.

Sets nothing in the environment itself; the entrypoint exports
ODIN_CAMERA_RELAY_SECRET_ROTATED=1 after this succeeds so the API does not rotate again.

Exit status:
  0   the configuration was written.
  1   it could not be written and ITAR mode is off: go2rtc starts with the file that is
      there and the API rotates and syncs after it starts, as before.
  10  it could not be written, ITAR mode is on, and the file was replaced with a
      configuration that has no camera sources. go2rtc may start: it has nothing to
      connect to until the API writes a checked configuration.
  11  ITAR mode is on and go2rtc could still have a camera source ODIN has not checked:
      the old file could not be replaced, or (go2rtc in its own container) the running
      go2rtc could not be restarted with, and shown to have loaded, the checked file. The
      entrypoint refuses to start the container.
  12  ITAR mode is on and GO2RTC_CONFIG names a different file from the one go2rtc is
      started with (LAUNCHED_CONFIG_PATH). ODIN would check one file and go2rtc would load
      another, so the entrypoint refuses to start the container.
Under ITAR mode the entrypoint treats every status other than 0 and 10 as a refusal.

go2rtc in its own container (the enterprise layout, ODIN_GO2RTC_API_URL): under ITAR mode,
at every start and whether or not the preparation succeeded, the running go2rtc is
restarted through its own API and must answer with the stream names of the file just
written (modules.printers.go2rtc_gate). If nothing is listening at its address it is not
running: the container starts, and the same restart-and-check is done before the first
request any ODIN process sends to go2rtc. With ITAR mode off none of this runs.
"""
import os
import sys

SAFE_TO_START_WITHOUT_CAMERAS = 10
STALE_CONFIGURATION_LEFT = 11
CONFIG_PATH_MISMATCH = 12
DEFAULT_CONFIG_PATH = "/app/go2rtc/go2rtc.yaml"   # modules.printers.route_utils.GO2RTC_CONFIG
# The file go2rtc is started with: `-config` in docker/supervisord.conf, and the shared
# go2rtc-config volume (mounted here at /app/go2rtc, in the go2rtc container at /config) in
# docker-compose.enterprise.yml. A contract test keeps this and those two files the same.
LAUNCHED_CONFIG_PATH = "/app/go2rtc/go2rtc.yaml"


def config_path() -> str:
    return os.environ.get("GO2RTC_CONFIG") or DEFAULT_CONFIG_PATH


def config_path_is_the_launched_one() -> bool:
    """True when ODIN writes the very directory entry go2rtc opens: the same file name in
    the same directory (the directory may be reached through a link). A link to the file
    under another name does not count: ODIN replaces files by renaming a new one into
    place, which would replace that link and leave the file go2rtc opens as it was."""
    ours, launched = os.path.abspath(config_path()), os.path.abspath(LAUNCHED_CONFIG_PATH)
    return (os.path.basename(ours) == os.path.basename(launched)
            and os.path.realpath(os.path.dirname(ours)) == os.path.realpath(os.path.dirname(launched)))


def empty_config(mark=None) -> str:
    """No camera source, no public STUN server, API and RTSP on loopback: the image's
    default (docker/go2rtc.yaml). Written as text so that it needs nothing that may be the
    reason the real configuration failed (database, relay secret, the go2rtc address check).
    `mark` (go2rtc in its own container) is one empty stream that shows which file go2rtc
    loaded; an empty stream has no source and connects to nothing."""
    from urllib.parse import urlsplit
    from modules.printers.go2rtc_access import DEFAULT_API_URL
    streams = "streams: {}\n" if mark is None else f"streams:\n  {mark}: []\n"
    return (
        "# Written by O.D.I.N. at container start: the camera configuration could not be prepared\n"
        "# and ITAR mode is on, so go2rtc starts with no camera sources.\n"
        + streams +
        "webrtc:\n"
        "  ice_servers: []\n"
        "  listen: \":8555\"\n"
        "api:\n"
        f"  listen: \"{urlsplit(DEFAULT_API_URL).netloc}\"\n"
        "rtsp:\n"
        "  listen: \"127.0.0.1:8554\"\n"
    )


def replace_with_empty_config(path: str, mark=None) -> bool:
    """Replace the go2rtc configuration at `path` with empty_config(). True only when the file
    then holds exactly that text. Tries an atomic replace first, then writes in place (a full
    volume has no room for a second file, but truncating the old one frees its space)."""
    try:
        content = empty_config(mark)
    except Exception:
        return False
    for in_place in (False, True):
        target = path if in_place else f"{path}.itar-empty.{os.getpid()}"
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if not in_place:
                os.replace(target, path)
            with open(path) as stream:
                if stream.read() == content:
                    _forget_applied_state(path)
                    return True
        except OSError:
            pass
        finally:
            if not in_place:
                try:
                    os.unlink(target)
                except OSError:
                    pass
    return False


def _forget_applied_state(path: str) -> None:
    """The sync state records which configuration go2rtc last loaded. Drop it (best effort)
    so the API's first sync reloads go2rtc instead of assuming the old one is running."""
    try:
        os.unlink(path + ".sync-state.json")
    except OSError:
        pass


def _separate_go2rtc() -> bool:
    """True when go2rtc is its own service (ODIN_GO2RTC_API_URL names another host). Raises
    when that address is refused under ITAR mode."""
    from modules.printers.go2rtc_access import go2rtc_is_remote
    return go2rtc_is_remote()


def _restart_separate_go2rtc(names, mark) -> bool:
    """Enterprise layout under ITAR mode. True when the running go2rtc was restarted and
    answered with the names of the file just written, or is not running (the gate stays
    pending and is passed on the first contact). False: the container must not start."""
    from modules.printers import go2rtc_gate
    try:
        go2rtc_gate.open_gate(names, mark)
        go2rtc_gate.pass_gate_now()
    except go2rtc_gate.Go2rtcNotRunning:
        print("  ! ODIN_ITAR_MODE=1: go2rtc is not running yet; it will be restarted with the checked camera "
              "configuration before O.D.I.N. asks it for anything", file=sys.stderr)
        return True
    except Exception as exc:
        print(f"  ✗ ODIN_ITAR_MODE=1: the running go2rtc could not be restarted with the checked camera "
              f"configuration ({type(exc).__name__}: {exc})", file=sys.stderr)
        return False
    print("  ✓ go2rtc restarted with the checked camera configuration")
    return True


def main() -> int:
    from core.itar import is_itar_mode
    itar = is_itar_mode()
    if itar and not config_path_is_the_launched_one():
        print(f"  ✗ ODIN_ITAR_MODE=1: GO2RTC_CONFIG is {config_path()} but go2rtc is started with "
              f"{LAUNCHED_CONFIG_PATH}. O.D.I.N. would check one file and go2rtc would load the other. "
              "Remove GO2RTC_CONFIG or set it to that path.", file=sys.stderr)
        return CONFIG_PATH_MISMATCH
    mark = None
    if itar:
        try:
            if _separate_go2rtc():
                from modules.printers.go2rtc_gate import new_mark
                mark = new_mark()
        except Exception as exc:
            print(f"  ✗ ODIN_ITAR_MODE=1: the go2rtc address is not usable ({type(exc).__name__})", file=sys.stderr)
            return STALE_CONFIGURATION_LEFT
    names, status = [], 0
    try:
        from core.schema.bootstrap import import_all_models
        import_all_models()  # the Printer mapper needs every related model registered
        from core.db import SessionLocal
        from modules.printers.camera_relay import rotate_relay_secret
        from modules.printers.route_utils import _build_go2rtc_config, write_go2rtc_config_before_start
        rotate_relay_secret()
        db = SessionLocal()
        try:
            config = _build_go2rtc_config(db)
            names = list(config.get("streams") or {})
            if mark is not None:
                config = {**config, "streams": {**(config.get("streams") or {}), mark: []}}
            write_go2rtc_config_before_start(config)
        finally:
            db.close()
    except Exception as exc:  # with ITAR mode off this never blocks start-up
        print(f"go2rtc pre-start config skipped: {type(exc).__name__}", file=sys.stderr)
        if not itar:
            return 1
        names = []
        if not replace_with_empty_config(config_path(), mark):
            print("  ✗ ODIN_ITAR_MODE=1: the old go2rtc configuration could not be replaced", file=sys.stderr)
            return STALE_CONFIGURATION_LEFT
        print("  ! ODIN_ITAR_MODE=1: cameras are off until the camera configuration can be written; "
              "go2rtc starts with no camera sources", file=sys.stderr)
        status = SAFE_TO_START_WITHOUT_CAMERAS
    if mark is not None and not _restart_separate_go2rtc(names, mark):
        return STALE_CONFIGURATION_LEFT
    if status == 0:
        print("  ✓ go2rtc configuration prepared")
    return status


if __name__ == "__main__":
    sys.exit(main())
