"""Camera URL validation, derivation, and credential masking."""
import logging
import re
from urllib.parse import quote as urlquote
from fastapi import HTTPException
import core.crypto as crypto
from modules.printers.bambu_chamber_camera import SOURCE as CHAMBER_CAMERA_SOURCE, chamber_source
log = logging.getLogger("odin.api")

# '&' is a query-string separator (cameras take ?user=&password=); the URL is only ever
# written to go2rtc's YAML as an rtsp(s) source, never to a shell.
_CAMERA_SHELL_METACHAR_RE = re.compile(r'[;|$`\\]')
# Discovered (HTTP/ffmpeg) sources also refuse '&': go2rtc ffmpeg sources parse directives.
_CAMERA_DIRECTIVE_RE = re.compile(r'[;&|$`\\]')


def _validate_camera_url(url: str) -> str:
    """Validate and sanitize a camera URL before writing to go2rtc config.

    - Only rtsp:// or rtsps:// schemes allowed
    - Strips shell metacharacters (; & | $ ` \\)
    - Rejects localhost/loopback targets (SSRF prevention)

    Returns the sanitized URL or raises HTTPException 400.
    """
    if not url:
        return url

    import urllib.parse
    from modules.printers.route_utils import _check_ssrf_blocklist
    stripped = url.strip()
    lower = stripped.lower()
    if not (lower.startswith("rtsp://") or lower.startswith("rtsps://")):
        raise HTTPException(status_code=400, detail="Camera URL must use rtsp:// or rtsps:// scheme")
    # Store the scheme lower-case so every later check sees one spelling.
    scheme_end = stripped.index("://")
    stripped = stripped[:scheme_end].lower() + stripped[scheme_end:]

    sanitized = _CAMERA_SHELL_METACHAR_RE.sub('', stripped)

    # Userinfo must be unambiguous: urlsplit ends the authority at the first
    # / ? #, so an unencoded @ / ? # in a password would make the parsed host
    # (and the masking) disagree with what the user meant.
    _prefix, userinfo, _rest = split_url_userinfo(sanitized)
    if userinfo is not None and any(ch in userinfo for ch in "@/?#"):
        raise HTTPException(status_code=400, detail=(
            "Camera URL username/password contains @ / ? or #; percent-encode them "
            "(%40 %2F %3F %23)"))
    try:
        parsed = urllib.parse.urlsplit(sanitized)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        raise HTTPException(status_code=400, detail="Camera URL is malformed")
    if not host:
        raise HTTPException(status_code=400, detail="Camera URL has no host")
    try:
        # With the port: ODIN's own service ports on its own addresses are refused.
        _check_ssrf_blocklist((f"[{host}]" if ":" in host else host) + (f":{port}" if port else ""))
    except HTTPException:
        raise HTTPException(status_code=400, detail="Camera URL points to a blocked host")

    return sanitized


# Bambu models with an RTSPS camera stream at :322/streaming/live/1 (auto
# camera URL). Bambu Studio's machine profiles carry no camera data, so a model
# is listed only with another real source; P2S, H2S, H2C and H2D Pro are NOT
# listed until verified on hardware (fixes.json records the claim text).
BAMBU_RTSPS_CAMERA_MODELS = frozenset({'X1C', 'X1 Carbon', 'X1E', 'X1 Carbon Combo', 'H2D'})


# ====================================================================
# Camera / go2rtc helpers
# ====================================================================

def get_camera_url(printer):
    """Stored/derived URL, or a credential-free chamber marker resolved only by the relay."""
    if printer.camera_url:
        url = printer.camera_url
        try:
            url = crypto.decrypt(url)
        except Exception:
            log.debug("Failed to decrypt camera URL (using raw)")
        return url
    if chamber_source(printer):
        return CHAMBER_CAMERA_SOURCE
    if printer.api_type == "bambu" and printer.api_key and printer.api_host:
        RTSP_MODELS = BAMBU_RTSPS_CAMERA_MODELS
        model = (printer.model or '').strip()
        if model not in RTSP_MODELS:
            return None
        try:
            parts = crypto.decrypt(printer.api_key).split("|")
            if len(parts) == 2:
                return f"rtsps://bblp:{urlquote(parts[1], safe='')}@{printer.api_host}:322/streaming/live/1"
        except Exception:
            log.debug("Failed to decrypt Bambu API key")
    return None


def effective_camera_url(printer):
    """Stored camera URL (possibly encrypted), else the one generated from the
    printer's credentials. Contains credentials: responses must mask it."""
    return printer.camera_url or get_camera_url(printer)


_URL_SCHEME_RE = re.compile(r'^([a-z][a-z0-9+.\-]*://)', re.IGNORECASE)
CAMERA_CREDENTIAL_MASK = '***'


_HOST_PORT_RE = re.compile(r'^(\[[0-9A-Fa-f:.%]+\]|[^\s:@\[\]/?#]+)(:[0-9]{0,5})?$')


def split_url_userinfo(url: str):
    """Split a URL into (scheme://, userinfo or None, text after the userinfo '@').

    Only the authority (up to the first / ? #, as urlsplit defines it) can
    carry userinfo, so an '@' in the path or query is never treated as a
    credential. The one exception is a legacy stored value whose unencoded
    / ? # in a password makes the authority not a valid host[:port] (e.g.
    "u:se/cret@host"): then everything up to the last '@' is treated as the
    credential, so masking never leaks part of it.
    """
    match = _URL_SCHEME_RE.match(url or '')
    if not match:
        return None, None, url
    rest = url[match.end():]
    end = min([i for i in (rest.find('/'), rest.find('?'), rest.find('#')) if i >= 0] or [len(rest)])
    authority = rest[:end]
    at = authority.rfind('@')
    if at >= 0:
        return match.group(1), rest[:at], rest[at + 1:]
    if '@' in rest and not _HOST_PORT_RE.match(authority):
        last = rest.rfind('@')
        return match.group(1), rest[:last], rest[last + 1:]
    return match.group(1), None, rest


# Query parameters some cameras take credentials in (?user=..&password=..).
_CREDENTIAL_QUERY_RE = re.compile(r'([?&](?:user|username|usr|login|password|pass|passwd|pwd|token|auth|key|secret|api_key|apikey|access_code|sig)=)([^&#]*)', re.IGNORECASE)
# Vendors that put the credential in the path. UniFi Protect: rtsp://host:7447/<token>,
# rtsps://host:7441/<token>[?enableSrtp]. Other path-token schemes are not recognised
# and are stored as entered (documented limitation: use userinfo or query credentials).
_PATH_TOKEN_RE = re.compile(r'^(rtsps?://[^/?#]+:(?:7441|7447)/)([A-Za-z0-9]{8,})(?=$|[?#])', re.IGNORECASE)
_DEFAULT_PORTS = {'rtsp': 554, 'rtsps': 322, 'rtspx': 322, 'http': 80, 'https': 443}


def camera_url_has_credentials(url) -> bool:
    """Userinfo, a credential query parameter or a path token: the URL must be stored encrypted."""
    if not isinstance(url, str):
        return False
    return (split_url_userinfo(url.strip())[1] is not None
            or any(m.group(2) for m in _CREDENTIAL_QUERY_RE.finditer(url))
            or bool(_PATH_TOKEN_RE.match(url.strip())))


def sanitize_camera_url(url: str) -> str:
    """Mask URL credentials (userinfo and credential query values) for API responses."""
    if not url:
        return url
    prefix, userinfo, rest = split_url_userinfo(url.strip())
    masked = url.strip() if userinfo is None else prefix + CAMERA_CREDENTIAL_MASK + '@' + rest
    masked = _PATH_TOKEN_RE.sub(lambda m: m.group(1) + CAMERA_CREDENTIAL_MASK, masked, count=1)
    return _CREDENTIAL_QUERY_RE.sub(lambda m: m.group(1) + (CAMERA_CREDENTIAL_MASK if m.group(2) else ''), masked)


def masked_camera_url(stored):
    """Response form of a stored camera URL: decrypted, credentials masked.

    Never returns ciphertext; an undecryptable stored value returns None.
    """
    if not stored:
        return stored
    plain = crypto.decrypt(stored) if crypto.is_encrypted(stored) else stored
    if not plain or crypto.is_encrypted(plain):
        return None
    return sanitize_camera_url(plain)


def _url_endpoint(url):
    """(scheme, host, port) with the scheme's default port filled in."""
    import urllib.parse
    try:
        parsed = urllib.parse.urlsplit(url)
        scheme = parsed.scheme.lower()
        return (scheme, (parsed.hostname or '').lower(), parsed.port or _DEFAULT_PORTS.get(scheme))
    except ValueError:
        return None


def camera_url_is_masked(url) -> bool:
    if not isinstance(url, str):
        return False
    return split_url_userinfo(url.strip())[1] == CAMERA_CREDENTIAL_MASK or any(
        m.group(2) == CAMERA_CREDENTIAL_MASK for m in _CREDENTIAL_QUERY_RE.finditer(url)) or bool(
        re.match(r'^rtsps?://[^/?#]+:(?:7441|7447)/\*\*\*(?=$|[?#])', url.strip(), re.IGNORECASE))


def resolve_masked_camera_url(incoming, stored):
    """Restore credentials when a client sends back a masked camera URL.

    Returns the plaintext URL to save, or None when the client sent back
    exactly the masked form of the stored URL (nothing changed). Stored
    credentials (userinfo and credential query values) are carried over only to
    the same scheme, host and port (default ports are equivalent; a path or
    other parameter change is allowed); pointing a masked URL at another
    endpoint, or masking a value with no stored counterpart, is a 400 that
    requires the full credentialed URL.
    """
    if not camera_url_is_masked(incoming):
        return incoming
    incoming = incoming.strip()
    plain = crypto.decrypt(stored) if stored and crypto.is_encrypted(stored) else stored
    if not plain or crypto.is_encrypted(plain):
        raise HTTPException(status_code=400, detail="Camera URL credentials are masked; enter the full URL")
    plain = plain.strip()
    if incoming == sanitize_camera_url(plain):
        return None
    prefix, userinfo, rest = split_url_userinfo(incoming)
    stored_prefix, stored_userinfo, stored_rest = split_url_userinfo(plain)
    if prefix is None or stored_prefix is None:
        raise HTTPException(status_code=400, detail="Camera URL credentials are masked; enter the full URL")
    # Compare endpoints with the userinfo removed, so a legacy password with
    # an unencoded / ? # cannot shift the parsed host (default ports are equivalent).
    target = _url_endpoint(prefix + rest)
    if target is None or target != _url_endpoint(stored_prefix + stored_rest):
        raise HTTPException(status_code=400,
                            detail="Camera URL endpoint changed; enter the full URL including credentials")
    resolved = incoming
    if userinfo == CAMERA_CREDENTIAL_MASK:
        if stored_userinfo is None:
            raise HTTPException(status_code=400, detail="Camera URL credentials are masked; enter the full URL")
        resolved = prefix.lower() + stored_userinfo + '@' + rest
    path_masked = re.match(r'^(rtsps?://[^/?#]+:(?:7441|7447)/)\*\*\*(?=$|[?#])', resolved, re.IGNORECASE)
    if path_masked:
        stored_path = _PATH_TOKEN_RE.match(plain)
        if not stored_path:
            raise HTTPException(status_code=400, detail="Camera URL credentials are masked; enter the full URL")
        resolved = path_masked.group(1) + stored_path.group(2) + resolved[path_masked.end():]
    stored_values = {m.group(1)[1:].lower(): m.group(2) for m in _CREDENTIAL_QUERY_RE.finditer(plain)}

    def restore(m):
        if m.group(2) != CAMERA_CREDENTIAL_MASK:
            return m.group(0)
        value = stored_values.get(m.group(1)[1:].lower())
        if not value:
            raise HTTPException(status_code=400, detail="Camera URL credentials are masked; enter the full URL")
        return m.group(1) + value
    resolved = _CREDENTIAL_QUERY_RE.sub(restore, resolved)
    return None if resolved == plain else resolved


