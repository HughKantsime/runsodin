"""Historical source copies excluded from live-code censuses, not restore bridges."""
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1] / 'backend'


def frozen_bundles(backend=BACKEND):
    """Discover every preserved release's immutable bundle or overlay."""
    return tuple(sorted(
        source
        for package in (backend / 'core/schema').glob('preserved*')
        if package.is_dir()
        for name in ('bundle', 'overlay')
        if (source := package / name).is_dir()
    ))


FROZEN_BUNDLES = frozen_bundles()


def is_frozen_source(path):
    return any(path.is_relative_to(bundle) for bundle in FROZEN_BUNDLES)
