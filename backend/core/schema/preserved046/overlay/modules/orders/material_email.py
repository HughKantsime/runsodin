"""Transient, conservative Amazon order preview. Never persist or fetch mail content."""
import hashlib
import re
import unicodedata
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr

from fastapi import HTTPException

MAX_BYTES = 300_000
ORDER = re.compile(r'^\d{3}-\d{7}-\d{7}$')
QUANTITY = re.compile(r'^Quantity:\s*([1-9]\d{0,3})$')


def _fail():
    raise HTTPException(422, 'Unsupported or ambiguous order email. Use the structured purchase preview.')


def _text(raw: str) -> str:
    try:
        data = raw.encode('utf-8')
    except UnicodeError:
        _fail()
    if len(data) > MAX_BYTES or '\x00' in raw:
        _fail()
    try:
        message = BytesParser(policy=policy.default).parsebytes(data)
    except (RecursionError, ValueError, TypeError):
        _fail()
    # Uploaded headers provide format hints only, never authenticated sender proof.
    if len(message.get_all('From', [])) != 1 or len(message.get_all('Subject', [])) != 1:
        _fail()
    if parseaddr(str(message.get('From', '')))[1].lower() != 'auto-confirm@amazon.com':
        _fail()
    if not re.match(r'^Ordered:', str(message.get('Subject', '')), re.I):
        _fail()
    queue = [(message, 0)]
    bodies = []
    count = 0
    while queue:
        part, depth = queue.pop()
        count += 1
        if depth > 8 or count > 40 or part.defects:
            _fail()
        if part.get_content_disposition() == 'attachment' or part.get_filename():
            continue
        kind = part.get_content_type()
        if kind == 'message/rfc822':
            continue
        if part.is_multipart():
            queue.extend((child, depth + 1) for child in part.iter_parts())
        elif kind == 'text/plain':
            try:
                body = part.get_content(errors='strict')
            except (LookupError, UnicodeError, ValueError):
                _fail()
            if part.defects:
                _fail()
            if not isinstance(body, str) or len(body.encode('utf-8')) > 100_000:
                _fail()
            bodies.append(body)
    if len(bodies) != 1:
        _fail()
    return bodies[0]


def preview(raw: str) -> dict:
    """Extract only order/title/ordered quantity; unknown formats fail closed."""
    body = _text(raw)
    # Normalize invisible formatting used by Amazon, not arbitrary fuzzy matching.
    lines = [''.join(c for c in unicodedata.normalize('NFC', line)
                     if unicodedata.category(c) != 'Cf').strip() for line in body.splitlines()]
    candidates = []
    order = None
    title = None
    expect_order = False
    seen = set()
    for line in lines:
        if not line:
            continue
        if line == 'Order #':
            if title is not None or expect_order:
                _fail()
            expect_order = True
            order = None
            continue
        if expect_order:
            if not ORDER.fullmatch(line):
                _fail()
            order = line
            expect_order = False
            continue
        if line.startswith('* '):
            if order is None or title is not None:
                _fail()
            title = line[2:].strip()
            continue
        if title is None:
            if line.startswith('Quantity:'):
                _fail()
            continue
        quantity = QUANTITY.fullmatch(line)
        if quantity:
            if not title or len(title) > 500 or re.search(r'https?://|@|[<>]', title, re.I):
                _fail()
            identity = (order, ' '.join(title.casefold().split()))
            if identity in seen or len(candidates) >= 50:
                _fail()
            seen.add(identity)
            fingerprint = hashlib.sha256((identity[0] + '\n' + identity[1]).encode()).hexdigest()
            candidates.append({'candidate_reference': fingerprint, 'vendor': 'Amazon',
                               'order_reference': order, 'title': title,
                               'ordered_quantity': int(quantity[1]), 'status': 'needs_mapping',
                               'message': 'Verify the exact listing, variant and pieces per pack before recording incoming material.'})
            title = None
            continue
        # Wrapped product lines are not accepted in this initial grammar. Other
        # boundaries may contain recipient/address/footer text and must not leak.
        _fail()
    if expect_order or title is not None or not candidates:
        _fail()
    return {'candidates': candidates, 'stock_changed': False, 'purchases_changed': False,
            'source_authenticated': False, 'message': 'Preview only. Uploaded sender headers are unverified. Ordered quantity is not received pieces.'}
