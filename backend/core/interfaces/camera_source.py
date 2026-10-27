"""Effective camera classification for API modules and standalone daemons."""
from types import SimpleNamespace


def camera_source(printer) -> str | None:
    from modules.printers.route_utils import get_camera_url
    get = printer.get if hasattr(printer, "get") else lambda key: getattr(printer, key, None)
    if get("camera_url"):
        return "configured"
    derived = get_camera_url(SimpleNamespace(
        camera_url=None, api_type=get("api_type"), api_key=get("api_key"),
        api_host=get("api_host"), model=get("model")))
    return "auto" if derived else None
