# Spec: Frontend Security Hardening

## Goal

Fix three frontend security issues: Three.js CDN import (supply chain risk), overly-broad CSP connect-src, and incomplete OIDC frontend flow (dead code + broken oidc_code exchange).

## Item 1: Bundle Three.js Locally (`frontend/src/components/ModelViewer.jsx`)

### Problem
`ModelViewer.jsx` dynamically imports Three.js from the Cloudflare CDN at runtime:
```js
const threeModule = await import('https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.module.js')
```
A CDN compromise means attacker JS runs in every authenticated session. Dynamic `import()` of external URLs cannot have SRI hashes. The `script-src 'self'` CSP should block this but browser behavior with ES module dynamic imports is inconsistent.

### Fix
1. `cd frontend && npm install three` (install the npm package)
2. In `ModelViewer.jsx`, replace the dynamic CDN import:
```js
// Remove: const threeModule = await import('https://cdnjs...')
// Replace with:
import * as THREE from 'three'
```
Adjust all references from `threeModule.` to `THREE.` accordingly, or keep as a named import.

Note: Three.js r128 is the specific version in the CDN URL. The npm package `three` should be pinned to the matching version for API compatibility. Check what APIs are used in ModelViewer.jsx and confirm compatibility with `three@0.128.0`.

## Item 2: Tighten CSP connect-src (`backend/main.py`)

### Problem
```python
"connect-src 'self' ws: wss:",
```
`ws:` and `wss:` without a host are wildcards allowing WebSocket connections to any origin. `'self'` alone covers same-origin WebSocket in compliant browsers.

### Fix
In `backend/main.py`, in `_CSP_DIRECTIVES`, change:
```python
"connect-src 'self' ws: wss:",
```
to:
```python
"connect-src 'self'",
```
`'self'` covers both HTTP and WebSocket connections to the same origin. No additional entries needed for the in-app WebSocket (`/ws`) or go2rtc streams (proxied through ODIN's own origin).

## Item 3: Remove Dead OIDC Token Code + Implement oidc_code Exchange (`frontend/src/pages/Login.jsx`, `frontend/src/api.js`)

### Problem A — Dead code block
`Login.jsx` has a dead code block checking for `?token=` in the URL (pre-cookie migration remnant). It does nothing useful — just reloads the page. It should be removed.

```js
// Remove this entire block:
if (urlToken) {
  setOidcLoading(true)
  window.history.replaceState({}, '', '/');
  window.location.reload();
}
```

### Problem B — OIDC code exchange never implemented
The backend OIDC callback redirects to `/?oidc_code=<one-time-code>`. The frontend should:
1. On load, check `window.location.search` for `?oidc_code=`
2. If found, POST it to `/api/auth/oidc/exchange` to get a session cookie set
3. Clear the `oidc_code` from the URL (`window.history.replaceState`)
4. Redirect to `/` (dashboard)

The backend `POST /auth/oidc/exchange` endpoint already exists — it accepts `{"code": "..."}` and returns the JWT (and sets the session cookie).

### Implementation in Login.jsx
```js
useEffect(() => {
  const urlParams = new URLSearchParams(window.location.search);
  const oidcCode = urlParams.get('oidc_code');
  if (oidcCode) {
    setOidcLoading(true);
    window.history.replaceState({}, '', '/');
    api.auth.oidcExchange(oidcCode)
      .then(() => { window.location.href = '/'; })
      .catch(() => { setError('SSO login failed. Please try again.'); setOidcLoading(false); });
  }
}, []);
```

Add `oidcExchange` to `frontend/src/api.js`:
```js
oidcExchange: (code) => fetchAPI('/auth/oidc/exchange', {
  method: 'POST',
  body: JSON.stringify({ code }),
}),
```

## Item 4: URL-encode OIDC error strings in backend (`backend/routers/auth.py`)

The OIDC error redirect uses raw error strings in the URL:
```python
return RedirectResponse(url=f"/?error={error}", ...)
```
Fix: URL-encode the error parameter:
```python
from urllib.parse import quote
return RedirectResponse(url=f"/?error={quote(str(error))}", ...)
```

## Item 5: Remove Google Fonts from Branding page (`frontend/src/pages/Branding.jsx`)

The Branding page injects a `<link>` to `fonts.googleapis.com` for font preview, violating the self-hosted fonts GDPR commitment. Remove the runtime Google Fonts load. Font previews on the Branding page can use the system fonts already loaded, or the operator can be told to enter a font name without live preview.

## Acceptance Criteria

- [ ] `ModelViewer.jsx` imports Three.js from npm, not CDN
- [ ] `npm run build` succeeds with local Three.js
- [ ] Model viewer still renders 3MF previews correctly
- [ ] `connect-src` in CSP no longer contains `ws:` or `wss:`
- [ ] Dead `urlToken` code block removed from Login.jsx
- [ ] OIDC `?oidc_code=` parameter handled in Login.jsx
- [ ] `api.auth.oidcExchange()` function exists in api.js
- [ ] OIDC error redirect URL-encodes the error string
- [ ] Google Fonts `<link>` removed from Branding.jsx
- [ ] `make test` passes (backend tests)

## Technical Notes

- Three.js version: the CDN URL uses r128 which corresponds to npm `three@0.128.0`. Install with `npm install three@0.128.0` to match the API exactly.
- The frontend build runs inside Docker (`make build`) — the npm install must happen in the Dockerfile or be pre-committed to `package.json`/`package-lock.json`
- `frontend/package.json` must be updated with the `three` dependency before `make build`
- Check `ModelViewer.jsx` carefully for the exact APIs used from the three module — `THREE.WebGLRenderer`, `THREE.Scene`, etc. — and verify they exist in r128
