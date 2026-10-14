"""Compatibility callers retain the single shared policy implementation."""
from core import timelapse_access as shared
from modules.archives import timelapse_access as compatibility
from modules.printers import camera_routes


def test_compatibility_exports_preserve_policy_identity():
    for name in (
        "_PROJECTION", "_TUPLE", "registry", "immutable_timelapse_policy",
        "timelapse_visibility", "assert_timelapse_access",
        "visible_printer_name", "inherit_timelapse_owner",
    ):
        assert getattr(compatibility, name) is getattr(shared, name)


def test_camera_routes_use_shared_policy():
    for name in (
        "timelapse_visibility", "assert_timelapse_access",
        "visible_printer_name", "inherit_timelapse_owner",
    ):
        assert getattr(camera_routes, name) is getattr(shared, name)
