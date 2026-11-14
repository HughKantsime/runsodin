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
  11  it could not be written, ITAR mode is on, and the old file could not be replaced
      either. The old file may name a camera outside the local network (it can date from
      before ITAR mode was turned on), so the entrypoint refuses to start the container.
  12  ITAR mode is on and GO2RTC_CONFIG names a different file from the one go2rtc is
      started with (LAUNCHED_CONFIG_PATH). ODIN would check one file and go2rtc would load
      another, so the entrypoint refuses to start the container.
Under ITAR mode the entrypoint treats every status other than 0 and 10 as a refusal.

What this covers is the FILE: under ITAR mode ODIN never leaves a camera source it has not
checked in the file go2rtc is started with. In the single container that is the whole
story, because supervisord starts go2rtc after this script, from that file.

go2rtc in its own container (the enterprise layout, ODIN_GO2RTC_API_URL) is not started or
stopped by ODIN, and ODIN cannot verify what it has loaded. Under ITAR mode, after the
checked file is written, this script asks that go2rtc once to restart
(POST /api/restart, which makes go2rtc v1.9.4 re-read its configuration). That is best
effort: a failure is a WARNING in the log telling the operator to restart the container by
hand, it changes no exit status, and nothing is recorded or claimed about the result.
With ITAR mode off none of this runs.
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


def empty_config() -> str:
    """No camera source, no public STUN server, API and RTSP on loopback: the image's
    default (docker/go2rtc.yaml). Written as text so that it needs nothing that may be the
    reason the real configuration failed (database, relay secret, the go2rtc address check)."""
    from urllib.parse import urlsplit
    from modules.printers.go2rtc_access import DEFAULT_API_URL
    return (
        "# Written by O.D.I.N. at container start: the camera configuration could not be prepared\n"
        "# and ITAR mode is on, so go2rtc starts with no camera sources.\n"
        "streams: {}\n"
        "webrtc:\n"
        "  ice_servers: []\n"
        "  listen: \":8555\"\n"
        "api:\n"
        f"  listen: \"{urlsplit(DEFAULT_API_URL).netloc}\"\n"
        "rtsp:\n"
        "  listen: \"127.0.0.1:8554\"\n"
    )


def replace_with_empty_config(path: str) -> bool:
    """Replace the go2rtc configuration at `path` with empty_config(). True only when the file
    then holds exactly that text. Tries an atomic replace first, then writes in place (a full
    volume has no room for a second file, but truncating the old one frees its space)."""
    try:
        content = empty_config()
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


RESTART_WARNING = ("  ! WARNING: ODIN_ITAR_MODE=1: the camera relay (go2rtc) runs as its own container and could "
                   "not be restarted{reason}. It may still hold the cameras it loaded before. Restart that "
                   "container by hand.")


def ask_separate_go2rtc_to_restart() -> None:
    """Enterprise layout under ITAR mode: one request to the separately run go2rtc to restart,
    so that it reads the file just written. Best effort: never raises, changes no exit
    status, records nothing. A failure, of any kind, is one WARNING line."""
    try:
        from modules.printers.go2rtc_access import go2rtc_is_remote
        if not go2rtc_is_remote():
            return   # go2rtc is started by supervisord, after this script, from the checked file
        from modules.printers.route_utils import _go2rtc_reload
        restarted = _go2rtc_reload()
    except Exception as exc:
        print(RESTART_WARNING.format(reason=f" ({type(exc).__name__})"), file=sys.stderr)
        return
    if restarted:
        print("  ✓ asked the separate camera relay (go2rtc) to restart with the checked camera configuration")
    else:
        print(RESTART_WARNING.format(reason=""), file=sys.stderr)


def main() -> int:
    from core.itar import is_itar_mode
    itar = is_itar_mode()
    if itar and not config_path_is_the_launched_one():
        print(f"  ✗ ODIN_ITAR_MODE=1: GO2RTC_CONFIG is {config_path()} but go2rtc is started with "
              f"{LAUNCHED_CONFIG_PATH}. O.D.I.N. would check one file and go2rtc would load the other. "
              "Remove GO2RTC_CONFIG or set it to that path.", file=sys.stderr)
        return CONFIG_PATH_MISMATCH
    status = 0
    try:
        from core.schema.bootstrap import import_all_models
        import_all_models()  # the Printer mapper needs every related model registered
        from core.db import SessionLocal
        from modules.printers.camera_relay import rotate_relay_secret
        from modules.printers.route_utils import _build_go2rtc_config, write_go2rtc_config_before_start
        rotate_relay_secret()
        db = SessionLocal()
        try:
            write_go2rtc_config_before_start(_build_go2rtc_config(db))
        finally:
            db.close()
    except Exception as exc:  # with ITAR mode off this never blocks start-up
        print(f"go2rtc pre-start config skipped: {type(exc).__name__}", file=sys.stderr)
        if not itar:
            return 1
        if not replace_with_empty_config(config_path()):
            print("  ✗ ODIN_ITAR_MODE=1: the old go2rtc configuration could not be replaced", file=sys.stderr)
            return STALE_CONFIGURATION_LEFT
        print("  ! ODIN_ITAR_MODE=1: cameras are off until the camera configuration can be written; "
              "go2rtc starts with no camera sources", file=sys.stderr)
        status = SAFE_TO_START_WITHOUT_CAMERAS
    if itar:
        ask_separate_go2rtc_to_restart()
    if status == 0:
        print("  ✓ go2rtc configuration prepared")
    return status


if __name__ == "__main__":
    sys.exit(main())
