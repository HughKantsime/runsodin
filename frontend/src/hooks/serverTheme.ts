import { fetchAPI } from '../api/client'

/**
 * Per-user theme preferences stored server-side (GET/PUT /api/auth/me/theme),
 * so a choice made on one device is restored on every other device.
 *
 * - `mode` (light/dark) is mirrored into localStorage('odin-theme') for a
 *   flash-free first paint; the server copy wins once it has loaded.
 * - `accent_color` overrides the branding accent for this user only. The
 *   backend answers its documented default (#6366f1) when the user never saved
 *   a theme; that default means "no personal override" and keeps the branding
 *   accent.
 */
export type ThemeMode = 'light' | 'dark'
export interface ServerTheme {
  accent_color?: string
  sidebar_style?: 'dark' | 'light'
  background?: string
  mode?: ThemeMode
}

export const SERVER_DEFAULT_ACCENT = '#6366f1'
export const THEME_EVENT = 'odin-theme-change'
const STORAGE_KEY = 'odin-theme'
const HEX = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/

let current: ServerTheme | null = null
let loading: Promise<ServerTheme | null> | null = null

export function applyUserAccent(theme: ServerTheme | null) {
  const root = document.documentElement
  const accent = theme?.accent_color
  if (typeof accent === 'string' && HEX.test(accent) && accent.toLowerCase() !== SERVER_DEFAULT_ACCENT) {
    // BrandingContext reads data-user-accent so a later branding load keeps it.
    root.dataset.userAccent = accent
    root.style.setProperty('--brand-accent', accent)
  } else {
    delete root.dataset.userAccent
  }
}

function applyMode(mode: unknown) {
  if (mode !== 'light' && mode !== 'dark') return
  try { localStorage.setItem(STORAGE_KEY, mode) } catch { /* storage unavailable */ }
  window.dispatchEvent(new CustomEvent<ThemeMode>(THEME_EVENT, { detail: mode }))
}

/** Load the signed-in user's theme once per page load and apply it. */
export function loadServerTheme(): Promise<ServerTheme | null> {
  if (!loading) {
    loading = fetchAPI<ServerTheme>('/auth/me/theme')
      .then(theme => {
        if (!theme || typeof theme !== 'object') return null
        current = theme
        applyUserAccent(theme)
        applyMode(theme.mode)
        return theme
      })
      .catch(() => { loading = null; return null })
  }
  return loading
}

/** Persist a light/dark choice to the account, keeping the other preferences. */
export async function saveServerMode(mode: ThemeMode): Promise<void> {
  const base = (await loadServerTheme()) ?? current ?? {}
  const next: ServerTheme = { ...base, mode }
  try {
    current = await fetchAPI<ServerTheme>('/auth/me/theme', { method: 'PUT', body: JSON.stringify(next) })
  } catch {
    // localStorage already holds the choice; the account copy syncs on the next toggle.
  }
}

/** Test seam: forget the per-page-load cache. */
export function resetServerThemeForTests() { current = null; loading = null }
