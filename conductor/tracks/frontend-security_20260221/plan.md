# Plan: Frontend Security Hardening

## Overview

Five targeted security fixes across frontend and backend. No architectural changes.

---

## Task 1: Bundle Three.js Locally
**File:** `frontend/package.json`, `frontend/src/components/ModelViewer.jsx`

- [x] Add `"three": "0.128.0"` to dependencies in `frontend/package.json`
- [x] Remove `loadThreeJS()` async CDN loader and the `let THREE = null` module-level variable
- [x] Add top-level static import: `import * as THREE from 'three'`
- [x] Remove `await loadThreeJS()` call inside the `init()` function

**Verify:** `package.json` has `three` in dependencies; import is static at top of file.

---

## Task 2: Tighten CSP connect-src
**File:** `backend/main.py`

- [x] Change `"connect-src 'self' ws: wss:"` to `"connect-src 'self'"`

**Verify:** String `ws:` no longer appears in `_CSP_DIRECTIVES`.

---

## Task 3: Remove dead urlToken code + implement oidc_code exchange
**Files:** `frontend/src/pages/Login.jsx`, `frontend/src/api.js`

- [x] In `Login.jsx`, replace the existing OIDC `useEffect` to:
  - Remove the `urlToken` dead code block entirely
  - Keep the `urlError` handling
  - Add `oidc_code` detection: read `?oidc_code=`, call `api.auth.oidcExchange(code)`, redirect to `/`
- [x] In `api.js`, add `oidcExchange` to the `auth` export object

**Verify:** No `urlToken` reference in Login.jsx; `oidcExchange` function exists in api.js.

---

## Task 4: URL-encode OIDC error redirects
**File:** `backend/routers/auth.py`

- [x] Add `from urllib.parse import quote` to imports
- [x] Replace `f"/?error={error}"` with `f"/?error={quote(str(error))}"` at all occurrences in `oidc_callback`

**Verify:** `quote(` appears in auth.py OIDC callback handler.

---

## Task 5: Remove Google Fonts CDN load from Branding page
**File:** `frontend/src/pages/Branding.jsx`

- [x] Remove the `BRANDING_FONTS_URL` constant
- [x] Remove the `useEffect` that appends the Google Fonts `<link>` tag to `document.head`
- [x] Remove the comment above that `useEffect`

**Verify:** No `googleapis.com` reference remains in Branding.jsx.

---

## Post-execution

- Run `make test` to verify backend tests pass (839+ passing)
- Bump version to 1.3.64
- Commit all changes
