from dataclasses import dataclass


@dataclass(frozen=True)
class UIRouteCase:
    name: str
    path: str
    ready_text: str | None = None
    ready_selector: str | None = None
    visual: bool = False
    requires_printer: bool = False
    min_api_calls: int = 1


@dataclass(frozen=True)
class PrinterStateCase:
    name: str
    gcode_state: str
    progress: int
    remaining: int
    layer: int
    total_layers: int
    expected_status: str
    bed_temp: int = 65
    bed_target: int = 70
    nozzle_temp: int = 215
    nozzle_target: int = 220
    hms: tuple[dict, ...] = ()
    last_seen_seconds_ago: int = 0
    expected_tv_status: str | None = None
    expected_overlay_status: str | None = None
    expected_progress_text: str | None = None


@dataclass(frozen=True)
class VisualCase:
    name: str
    route_name: str
    route_path: str
    state_name: str | None = None
    requires_printer: bool = False


UI_AUDIT_ROUTES = [
    UIRouteCase("dashboard", "/", ready_text="Dashboard", visual=True),
    UIRouteCase("timeline", "/timeline", ready_text="Timeline"),
    UIRouteCase("jobs", "/jobs", ready_text="Jobs", visual=True),
    UIRouteCase("printers", "/printers", ready_text="Printers", visual=True),
    UIRouteCase("printer-detail", "/printers/{printer_id}", ready_text="Loaded Filaments", requires_printer=True),
    UIRouteCase("models", "/models", ready_text="Models", visual=True),
    UIRouteCase("profiles", "/profiles", ready_text="Profiles"),
    UIRouteCase("calculator", "/calculator", ready_text="Cost Calculator"),
    UIRouteCase("analytics", "/analytics", ready_text="Analytics"),
    UIRouteCase("utilization", "/utilization", ready_text="Utilization"),
    UIRouteCase("upload", "/upload", ready_text="Upload"),
    UIRouteCase("spools", "/spools", ready_text="Spools", visual=True),
    UIRouteCase("consumables", "/consumables", ready_text="Consumables"),
    UIRouteCase("settings", "/settings", ready_text="Settings", visual=True),
    UIRouteCase("maintenance", "/maintenance", ready_text="Maintenance"),
    UIRouteCase("cameras", "/cameras", ready_text="Cameras"),
    UIRouteCase("camera-detail", "/cameras/{printer_id}", ready_text="Printer Info", requires_printer=True),
    UIRouteCase("products", "/products", ready_text="Products", visual=True),
    UIRouteCase("orders", "/orders", ready_text="Orders", visual=True),
    UIRouteCase("alerts", "/alerts", ready_text="Alerts"),
    UIRouteCase("detections", "/detections", ready_text="Detections"),
    UIRouteCase("education-reports", "/education-reports", ready_text="Education Reports"),
    UIRouteCase("timelapses", "/timelapses", ready_text="Timelapses"),
    UIRouteCase("archives", "/archives", ready_text="Archives"),
    UIRouteCase("projects", "/projects", ready_text="Projects"),
    UIRouteCase("print-log", "/print-log", ready_text="Print Log"),
    UIRouteCase("audit", "/audit", ready_text="Audit"),
    UIRouteCase("tv", "/tv", ready_selector='[data-testid="tv-printer-card"]', requires_printer=True),
    UIRouteCase("overlay", "/overlay/{printer_id}?camera=false", ready_selector='[data-testid="overlay-printer-name"]', requires_printer=True, min_api_calls=0),
]

VISUAL_UI_ROUTE_NAMES = {route.name for route in UI_AUDIT_ROUTES if route.visual}

PRINTER_STATE_CASES = [
    PrinterStateCase(
        name="online-idle",
        gcode_state="IDLE",
        progress=0,
        remaining=0,
        layer=0,
        total_layers=0,
        expected_status="Online",
        expected_tv_status="Idle",
        expected_overlay_status="Idle",
    ),
    PrinterStateCase(
        name="printing-start",
        gcode_state="RUNNING",
        progress=0,
        remaining=90,
        layer=1,
        total_layers=100,
        expected_status="Online",
        expected_tv_status="Printing",
        expected_overlay_status="Printing",
        expected_progress_text="0%",
    ),
    PrinterStateCase(
        name="printing-mid",
        gcode_state="RUNNING",
        progress=50,
        remaining=45,
        layer=50,
        total_layers=100,
        expected_status="Online",
        expected_tv_status="Printing",
        expected_overlay_status="Printing",
        expected_progress_text="50%",
    ),
    PrinterStateCase(
        name="paused-mid",
        gcode_state="PAUSE",
        progress=50,
        remaining=45,
        layer=50,
        total_layers=100,
        expected_status="Online",
        expected_tv_status="Printing",
        expected_overlay_status="Paused",
        expected_progress_text="50%",
    ),
    PrinterStateCase(
        name="print-complete",
        gcode_state="FINISH",
        progress=100,
        remaining=0,
        layer=100,
        total_layers=100,
        expected_status="Online",
        expected_tv_status="Idle",
        expected_overlay_status="Complete",
        expected_progress_text="100%",
    ),
    PrinterStateCase(
        name="bed-temp-warning",
        gcode_state="IDLE",
        progress=0,
        remaining=0,
        layer=0,
        total_layers=0,
        expected_status="Online",
        bed_temp=90,
        bed_target=100,
        nozzle_temp=35,
        nozzle_target=0,
        expected_tv_status="Idle",
        expected_overlay_status="Idle",
    ),
    PrinterStateCase(
        name="filament-runout",
        gcode_state="RUNNING",
        progress=62,
        remaining=21,
        layer=62,
        total_layers=100,
        expected_status="Online",
        hms=({"attr": 0x05010500, "code": 0x00010001},),
        expected_tv_status="Error",
        expected_overlay_status="Printing",
        expected_progress_text="62%",
    ),
    PrinterStateCase(
        name="offline",
        gcode_state="IDLE",
        progress=0,
        remaining=0,
        layer=0,
        total_layers=0,
        expected_status="Offline",
        last_seen_seconds_ago=300,
        expected_tv_status="Offline",
        expected_overlay_status="Idle",
    ),
]

VISUAL_CASES = [
    VisualCase("dashboard-idle", "dashboard", "/", "online-idle"),
    VisualCase("printers-idle", "printers", "/printers", "online-idle", requires_printer=True),
    VisualCase("printers-printing-mid", "printers", "/printers", "printing-mid", requires_printer=True),
    VisualCase("printers-filament-runout", "printers", "/printers", "filament-runout", requires_printer=True),
    VisualCase("overlay-printing-mid", "overlay", "/overlay/{printer_id}?camera=false", "printing-mid", requires_printer=True),
    VisualCase("overlay-complete", "overlay", "/overlay/{printer_id}?camera=false", "print-complete", requires_printer=True),
    VisualCase("tv-printing-mid", "tv", "/tv", "printing-mid", requires_printer=True),
    VisualCase("tv-filament-runout", "tv", "/tv", "filament-runout", requires_printer=True),
    VisualCase("settings-default", "settings", "/settings"),
]

APP_ROUTE_PATHS = {
    "/",
    "/timeline",
    "/jobs",
    "/printers",
    "/printers/{printer_id}",
    "/models",
    "/profiles",
    "/calculator",
    "/analytics",
    "/utilization",
    "/upload",
    "/spools",
    "/tv",
    "/consumables",
    "/settings",
    "/maintenance",
    "/cameras",
    "/cameras/{printer_id}",
    "/products",
    "/orders",
    "/alerts",
    "/detections",
    "/education-reports",
    "/timelapses",
    "/archives",
    "/projects",
    "/print-log",
    "/audit",
    "/overlay/{printer_id}",
}

READ_SKIP_PATHS = {
    "/ws",
    "/health",
    "/api/admin/logs/stream",
    "/api/spoolman/filaments",  # requires external spoolman service
}

READ_SKIP_PREFIXES = (
    "/openapi.json",
    "/api/v1/openapi.json",
    "/api/v1/docs",
    "/api/v1/redoc",
    "/api/vision/frames/",
)

DOWNLOAD_OR_STREAM_PATTERNS = (
    "/stream",
    "/export",
    "invoice.pdf",
    "/preview",
    "/live-status",
    "/ams/current",
    "/ams/environment",
    "/hms-history",
    "/nozzle",
    "/webrtc",
)

INVALID_WRITE_SKIP_PREFIXES = (
    "/api/license",
    "/api/backup",
)

INVALID_WRITE_SKIP_PATHS = {
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/refresh",
    "/api/setup/admin",
    "/api/setup/printer",
    "/api/setup/test-printer",
    "/api/setup/complete",
    "/api/setup/clear",
}


def route_map() -> dict[str, UIRouteCase]:
    return {route.name: route for route in UI_AUDIT_ROUTES}


def printer_state_map() -> dict[str, PrinterStateCase]:
    return {state.name: state for state in PRINTER_STATE_CASES}


def canonical_ui_path(path: str) -> str:
    return path.replace(":id", "{printer_id}").replace(":printerId", "{printer_id}")


def canonical_api_path(path: str) -> str:
    if path.startswith("/api/v1/"):
        return "/api/" + path[len("/api/v1/"):]
    return path


def is_download_or_stream(path: str) -> bool:
    return any(pattern in path for pattern in DOWNLOAD_OR_STREAM_PATTERNS)
