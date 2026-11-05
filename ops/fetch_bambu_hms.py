#!/usr/bin/env python3
"""Release-only Bambu list fetcher. Never imported by runtime application code."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from modules.printers.bambu_hms_bundle import MAX_BYTES, payload_bytes, validate_response

# Legacy serial families plus Bambu Studio HMS.cpp package_dev_id_types.
# Fetch the full upstream family set (a superset of ODIN's models); runtime
# selects directly by serial prefix and never needs a model-name guess.
PREFIXES = ('00M', '00W', '03W', '01S', '01P', '030', '039',
            '094', '239', '093', '20P', '22E', '31B', '26A')
TIMEOUT = 20


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Bambu list redirects are not allowed')


def fetch(prefix, opener=None):
    import re
    if not re.fullmatch(r'[A-Z0-9]{3}', prefix):
        raise ValueError('Expected a three-character model prefix')
    url = f'https://e.bambulab.com/query.php?lang=en&d={prefix}'
    opener = opener or urllib.request.build_opener(NoRedirect())
    request = urllib.request.Request(url, headers={'Accept': 'application/json', 'Accept-Encoding': 'identity', 'User-Agent': 'ODIN-release-fetch/1.0'})
    with opener.open(request, timeout=TIMEOUT) as response:
        if response.status != 200:
            raise ValueError('Bambu list HTTP status is not 200')
        raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('Bambu list exceeds size limit')
    payload = json.loads(raw)
    validate_response(payload)
    return dict(prefix=prefix, version=payload['ver'], fetched_at=datetime.now(timezone.utc).isoformat(),
                source_url=url, sha256=hashlib.sha256(payload_bytes(payload)).hexdigest(), response=payload)


def write_bundle(destination, prefixes, opener=None):
    # Validate every response before replacing any last-good release data.
    bundles = {prefix: fetch(prefix, opener) for prefix in sorted(set(prefixes))}
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    index = {'format': 1, 'lists': {p: {k: v for k, v in b.items() if k != 'response'} for p, b in bundles.items()}}
    for name, value in [(f'{p}.json', b) for p, b in bundles.items()] + [('index.json', index)]:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=destination, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        try:
            temporary.chmod(0o644)  # Public vendor data must be readable by the image's non-root user.
            temporary.replace(destination / name)
        finally:
            temporary.unlink(missing_ok=True)
    return index


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'backend/data/bambu_hms')
    parser.add_argument('--prefix', action='append', default=[],
                        help='Additional serial family to include with the upstream set')
    args = parser.parse_args()
    try:
        index = write_bundle(args.output, (*PREFIXES, *args.prefix))
    except Exception as exc:
        # Never emit response bodies, credentials or arbitrary exception messages.
        parser.exit(1, f'Bambu list fetch failed ({type(exc).__name__}); release data was not refreshed.\n')
    print(f"Wrote {len(index['lists'])} bundled Bambu lists to {args.output}")


if __name__ == '__main__':
    main()
