"""Effective-camera rule shared by the vision monitor and the Vigil AI settings API.

A printer has a camera for vision when it stores a camera URL, or when one is derived
from its credentials (Bambu RTSPS or port-6000 chamber camera) — the same resolution
go2rtc sync uses (modules.printers.route_utils.get_camera_url). No cv2 import here.
"""
import logging

log = logging.getLogger('vision_monitor')


def camera_source(printer) -> str | None:
    """'configured', 'auto', or None; `printer` is a mapping or an object with printer columns."""
    from core.interfaces.camera_source import camera_source as classify
    try:
        return classify(printer)
    except Exception as e:
        get = printer.get if hasattr(printer, 'get') else lambda key: getattr(printer, key, None)
        log.error("Camera resolution failed for printer %s: %s", get('id'), type(e).__name__)
        return None
