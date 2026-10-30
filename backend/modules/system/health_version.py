"""Backend version lookup shared by system health and license responses."""
from pathlib import Path


def backend_version():
    version_file = Path(__file__).parent.parent.parent.parent / "VERSION"
    if version_file.exists():
        return version_file.read_text().strip()
    try:
        from main import __version__
    except ImportError:
        return "0.0.0"
    return __version__
