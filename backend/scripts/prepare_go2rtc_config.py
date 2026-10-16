"""Container start (docker/entrypoint.sh), before supervisord: rotate the per-boot camera
relay secret (which also derives go2rtc's RTSP credentials) and write the go2rtc config,
so go2rtc starts with this boot's configuration instead of being restarted by the API.

Sets nothing in the environment itself; the entrypoint exports
ODIN_CAMERA_RELAY_SECRET_ROTATED=1 after this succeeds so the API does not rotate again.
Exit status 0 on success, 1 on any failure (the API then rotates and syncs as before).
"""
import sys


def main() -> int:
    try:
        from core.db import SessionLocal
        from modules.printers.camera_relay import rotate_relay_secret
        from modules.printers.route_utils import _build_go2rtc_config, write_go2rtc_config_before_start
        rotate_relay_secret()
        db = SessionLocal()
        try:
            write_go2rtc_config_before_start(_build_go2rtc_config(db))
        finally:
            db.close()
    except Exception as exc:  # never blocks start-up
        print(f"go2rtc pre-start config skipped: {type(exc).__name__}", file=sys.stderr)
        return 1
    print("  ✓ go2rtc configuration prepared")
    return 0


if __name__ == "__main__":
    sys.exit(main())
