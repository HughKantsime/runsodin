"""Build the real app against an owned frontend tree, independent of local builds."""
from unittest.mock import patch

from tests.test_contracts import test_cert_G_harness as harness


def run_probe(name, code, tmp_path, *, frontend_dist):
    setup = r'''
import core.app as app_module
from unittest.mock import patch
frontend_root = WORK / 'app-tree'
fake_app_file = frontend_root / 'backend' / 'core' / 'app.py'
fake_app_file.parent.mkdir(parents=True)
dist = frontend_root / 'frontend' / 'dist'
if FRONTEND_PRESENT:
 (dist / 'assets').mkdir(parents=True)
 (dist / 'index.html').write_text('<html>Owned probe frontend</html>')
with patch.object(app_module, '__file__', str(fake_app_file)):
 app = create_app()
assert any(getattr(route, 'path', None) == '/{path:path}' for route in app.routes) == FRONTEND_PRESENT
'''.replace('FRONTEND_PRESENT', repr(frontend_dist))
    prelude = harness.PRELUDE.replace('app = create_app()', setup)
    with patch.object(harness, 'PRELUDE', prelude):
        return harness.run_probe(name, code, tmp_path)
