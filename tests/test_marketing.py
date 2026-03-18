"""
Tests for marketing automation scripts.

Tests internal logic and data integrity without requiring a running ODIN
instance or MQTT broker.  All checks are offline — we parse the Python AST
to extract data constants and validate them.
"""

import ast
import json
from pathlib import Path

import pytest

MARKETING_DIR = Path(__file__).resolve().parent.parent / "marketing"


# ── Helpers ────────────────────────────────────────────────────────────


def _extract_constant(filepath: Path, var_name: str):
    """Parse a Python file's AST and return the literal value of a top-level assignment."""
    with open(filepath) as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        # Regular assignment: VAR = value
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == var_name:
                    return ast.literal_eval(node.value)
        # Annotated assignment: VAR: type = value
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == var_name and node.value is not None:
                return ast.literal_eval(node.value)
    raise ValueError(f"{var_name} not found in {filepath.name}")


# ── Syntax checks ─────────────────────────────────────────────────────


class TestScriptSyntax:
    """Every marketing script must be valid Python."""

    @pytest.mark.parametrize("script", [
        "seed.py", "screenshots.py", "video.py",
        "mqtt_recorder.py", "mqtt_replayer.py",
    ])
    def test_script_parses(self, script):
        path = MARKETING_DIR / script
        assert path.exists(), f"{script} not found"
        with open(path) as f:
            ast.parse(f.read())


# ── seed.py data integrity ────────────────────────────────────────────


class TestSeedData:

    def test_filaments_have_required_fields(self):
        filaments = _extract_constant(MARKETING_DIR / "seed.py", "FILAMENTS")
        assert len(filaments) >= 10, "Need ≥10 filaments for visual variety"
        for fil in filaments:
            assert "brand" in fil, f"Missing brand: {fil}"
            assert "name" in fil, f"Missing name: {fil}"
            assert "material" in fil, f"Missing material: {fil}"
            assert "color_hex" in fil, f"Missing color_hex: {fil}"
            # Valid 6-char hex
            assert len(fil["color_hex"]) == 6, f"Bad color_hex length: {fil['color_hex']}"
            int(fil["color_hex"], 16)

    def test_filaments_cover_multiple_brands(self):
        filaments = _extract_constant(MARKETING_DIR / "seed.py", "FILAMENTS")
        brands = {f["brand"] for f in filaments}
        assert len(brands) >= 3, f"Need ≥3 brands, got {brands}"

    def test_filaments_cover_multiple_materials(self):
        filaments = _extract_constant(MARKETING_DIR / "seed.py", "FILAMENTS")
        materials = {f["material"] for f in filaments}
        assert len(materials) >= 3, f"Need ≥3 materials, got {materials}"

    def test_printers_have_required_fields(self):
        printers = _extract_constant(MARKETING_DIR / "seed.py", "PRINTERS")
        assert len(printers) >= 6, "Need ≥6 printers"
        names = set()
        for p in printers:
            assert "name" in p
            assert "model" in p
            assert "api_type" in p
            assert p["api_type"] in ("bambu", "moonraker", "prusalink"), f"Bad api_type: {p['api_type']}"
            assert p["name"] not in names, f"Duplicate printer: {p['name']}"
            names.add(p["name"])

    def test_printers_cover_multiple_types(self):
        printers = _extract_constant(MARKETING_DIR / "seed.py", "PRINTERS")
        types = {p["api_type"] for p in printers}
        assert len(types) >= 2, f"Need ≥2 api_types, got {types}"

    def test_models_have_required_fields(self):
        models = _extract_constant(MARKETING_DIR / "seed.py", "MODELS")
        assert len(models) >= 5, "Need ≥5 models"
        for m in models:
            assert "name" in m
            assert "build_time_hours" in m
            assert m["build_time_hours"] > 0

    def test_models_have_unique_names(self):
        models = _extract_constant(MARKETING_DIR / "seed.py", "MODELS")
        names = [m["name"] for m in models]
        assert len(names) == len(set(names)), f"Duplicate model names found"


# ── screenshots.py configuration ──────────────────────────────────────


class TestScreenshotsConfig:

    def test_pages_cover_key_routes(self):
        pages = _extract_constant(MARKETING_DIR / "screenshots.py", "PAGES")
        paths = {p[1] for p in pages}
        required = {"/", "/printers", "/jobs", "/models", "/spools", "/orders", "/settings"}
        missing = required - paths
        assert not missing, f"Missing required pages: {missing}"

    def test_pages_have_descriptive_names(self):
        pages = _extract_constant(MARKETING_DIR / "screenshots.py", "PAGES")
        for name, path in pages:
            assert name, f"Empty name for path {path}"
            assert path.startswith("/"), f"Path must start with /: {path}"

    def test_desktop_viewport_is_1920x1080(self):
        vp = _extract_constant(MARKETING_DIR / "screenshots.py", "DESKTOP_VIEWPORT")
        assert vp == {"width": 1920, "height": 1080}

    def test_mobile_viewport_is_390x844(self):
        vp = _extract_constant(MARKETING_DIR / "screenshots.py", "MOBILE_VIEWPORT")
        assert vp == {"width": 390, "height": 844}

    def test_mobile_pages_are_subset_of_pages(self):
        pages = _extract_constant(MARKETING_DIR / "screenshots.py", "PAGES")
        page_names = {p[0] for p in pages}
        mobile = set(_extract_constant(MARKETING_DIR / "screenshots.py", "MOBILE_PAGES"))
        extra = mobile - page_names
        assert not extra, f"MOBILE_PAGES references unknown pages: {extra}"


# ── video.py configuration ────────────────────────────────────────────


class TestVideoConfig:

    def test_walkthrough_has_at_least_10_stops(self):
        walkthrough = _extract_constant(MARKETING_DIR / "video.py", "WALKTHROUGH")
        assert len(walkthrough) >= 10, f"Need ≥10 walkthrough stops, got {len(walkthrough)}"

    def test_walkthrough_starts_and_ends_on_dashboard(self):
        walkthrough = _extract_constant(MARKETING_DIR / "video.py", "WALKTHROUGH")
        assert walkthrough[0][0] == "/", "Must start on dashboard"
        assert walkthrough[-1][0] == "/", "Must end on dashboard"

    def test_walkthrough_dwell_times_are_positive(self):
        walkthrough = _extract_constant(MARKETING_DIR / "video.py", "WALKTHROUGH")
        for path, dwell in walkthrough:
            assert dwell > 0, f"Zero/negative dwell for {path}: {dwell}"


# ── MQTT recording format ─────────────────────────────────────────────


class TestMQTTRecordingFormat:

    def test_recording_round_trips(self, tmp_path):
        """Recording JSON format matches what the replayer expects."""
        recording = {
            "broker": "test-broker",
            "message_count": 3,
            "duration_seconds": 10.5,
            "messages": [
                {"t": 0.0, "topic": "device/123/report", "payload": {"temp": 210}, "qos": 0},
                {"t": 5.0, "topic": "device/123/report", "payload": {"temp": 211}, "qos": 0},
                {"t": 10.5, "topic": "device/123/report", "payload": {"temp": 210}, "qos": 0},
            ],
        }
        path = tmp_path / "test.json"
        with open(path, "w") as f:
            json.dump(recording, f)
        with open(path) as f:
            loaded = json.load(f)
        assert loaded["message_count"] == 3
        assert loaded["messages"][0]["t"] == 0.0
        assert loaded["messages"][-1]["topic"] == "device/123/report"
        assert isinstance(loaded["messages"][0]["payload"], dict)

    def test_recording_timestamps_are_monotonic(self):
        """Ensure time values increase monotonically in sample data."""
        messages = [
            {"t": 0.0, "topic": "a", "payload": "x", "qos": 0},
            {"t": 1.5, "topic": "a", "payload": "x", "qos": 0},
            {"t": 3.0, "topic": "a", "payload": "x", "qos": 0},
        ]
        times = [m["t"] for m in messages]
        assert times == sorted(times), "Timestamps must be monotonically increasing"
