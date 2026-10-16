"""Effective-camera rule shared by the vision monitor and the Vigil AI settings API.

A printer has a camera for vision when it stores a camera URL, or when one is derived
from its credentials (auto-configured Bambu X1C/X1E/H2D RTSP) — the same resolution
go2rtc sync uses (modules.printers.route_utils.get_camera_url). No cv2 import here.
"""
import logging
from types import SimpleNamespace

log = logging.getLogger('vision_monitor')


def camera_source(printer) -> str | None:
    """'configured', 'auto', or None; `printer` is a mapping or an object with printer columns."""
    get = printer.get if hasattr(printer, 'get') else lambda key: getattr(printer, key, None)
    if get('camera_url'):
        return 'configured'
    from modules.printers.route_utils import get_camera_url
    try:
        derived = get_camera_url(SimpleNamespace(
            camera_url=None, api_type=get('api_type'), api_key=get('api_key'),
            api_host=get('api_host'), model=get('model')))
    except Exception as e:
        log.error(f"Camera URL resolution failed for printer {get('id')}: {e}")
        return None
    return 'auto' if derived else None
