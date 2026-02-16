import { createContext, useContext, useState, useEffect } from "react"

const API_BASE = '/api'

// Default branding - matches current hardcoded Tailwind config
const DEFAULT_BRANDING = {
  app_name: "O.D.I.N.",
  app_subtitle: "Print Farm Management",
  // Colors
  primary_color: "#22c55e",
  accent_color: "#4ade80",
  sidebar_bg: "#1a1917",
  sidebar_border: "#3b3934",
  sidebar_text: "#8a8679",
  sidebar_active_bg: "#3b3934",
  sidebar_active_text: "#4ade80",
  content_bg: "#1a1917",
  card_bg: "#33312d",
  card_border: "#3b3934",
  text_primary: "#e5e4e1",
  text_secondary: "#8a8679",
  text_muted: "#58554a",
  input_bg: "#3b3934",
  input_border: "#47453d",
  // Fonts
  font_display: "system-ui, -apple-system, sans-serif",
  font_body: "system-ui, -apple-system, sans-serif",
  font_mono: "ui-monospace, monospace",
  // Assets
  logo_url: null,
  favicon_url: null,
  footer_text: "System Online",
  support_url: null,
}

const BrandingContext = createContext(DEFAULT_BRANDING)

export function BrandingProvider({ children }) {
  const [branding, setBranding] = useState(DEFAULT_BRANDING)

  useEffect(() => {
    fetch(`${API_BASE}/branding`)
      .then(res => {
        if (res.ok) return res.json()
        throw new Error("Branding fetch failed")
      })
      .then(data => {
        const merged = { ...DEFAULT_BRANDING, ...data }
        setBranding(merged)
        applyBrandingCSS(merged)
        applyBrandingMeta(merged)
      })
      .catch(() => {
        // Use defaults silently - branding table may not exist yet
        applyBrandingCSS(DEFAULT_BRANDING)
      })
  }, [])

  // Re-apply branding when theme toggles (html.light class changes)
  useEffect(() => {
    const observer = new MutationObserver(() => {
      applyBrandingCSS(branding)
    })
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class"],
    })
    return () => observer.disconnect()
  }, [branding])

  return (
    <BrandingContext.Provider value={branding}>
      {children}
    </BrandingContext.Provider>
  )
}

export function useBranding() {
  return useContext(BrandingContext)
}

/**
 * Inject CSS custom properties onto :root.
 * Every branded surface reads from these vars so changing
 * them here re-themes the entire app instantly.
 *
 * In light mode, only theme-neutral properties (fonts, primary/accent)
 * are applied as inline styles. Surface colors (backgrounds, text, inputs)
 * are left to the html.light CSS block in index.css, which would otherwise
 * be clobbered by inline style specificity.
 */
function applyBrandingCSS(b) {
  const root = document.documentElement
  const isLight = root.classList.contains("light")

  // Accent / brand — always apply
  root.style.setProperty("--brand-primary", isLight ? "#b45309" : b.primary_color)
  root.style.setProperty("--brand-accent", isLight ? "#d97706" : b.accent_color)

  // Fonts — always apply
  root.style.setProperty("--brand-font-display", b.font_display)
  root.style.setProperty("--brand-font-body", b.font_body)
  root.style.setProperty("--brand-font-mono", b.font_mono)

  if (isLight) {
    // In light mode, remove any inline surface overrides so the
    // html.light {} CSS block takes effect without specificity fights.
    const surfaceVars = [
      "--brand-sidebar-bg", "--brand-sidebar-border", "--brand-sidebar-text",
      "--brand-sidebar-active-bg", "--brand-sidebar-active-text",
      "--brand-content-bg", "--brand-card-bg", "--brand-card-border",
      "--brand-text-primary", "--brand-text-secondary", "--brand-text-muted",
      "--brand-input-bg", "--brand-input-border",
    ]
    surfaceVars.forEach(v => root.style.removeProperty(v))
  } else {
    // Dark mode — apply all surface properties inline
    root.style.setProperty("--brand-sidebar-bg", b.sidebar_bg)
    root.style.setProperty("--brand-sidebar-border", b.sidebar_border)
    root.style.setProperty("--brand-sidebar-text", b.sidebar_text)
    root.style.setProperty("--brand-sidebar-active-bg", b.sidebar_active_bg)
    root.style.setProperty("--brand-sidebar-active-text", b.sidebar_active_text)
    root.style.setProperty("--brand-content-bg", b.content_bg)
    root.style.setProperty("--brand-card-bg", b.card_bg)
    root.style.setProperty("--brand-card-border", b.card_border)
    root.style.setProperty("--brand-text-primary", b.text_primary)
    root.style.setProperty("--brand-text-secondary", b.text_secondary)
    root.style.setProperty("--brand-text-muted", b.text_muted)
    root.style.setProperty("--brand-input-bg", b.input_bg)
    root.style.setProperty("--brand-input-border", b.input_border)
  }
}



/**
 * Update document title and favicon.
 */
function applyBrandingMeta(b) {
  if (b.app_name) {
    document.title = b.app_subtitle
      ? `${b.app_name} ${b.app_subtitle}`
      : b.app_name
  }

  if (b.favicon_url) {
    let link = document.querySelector("link[rel~='icon']")
    if (!link) {
      link = document.createElement("link")
      link.rel = "icon"
      document.head.appendChild(link)
    }
    link.href = b.favicon_url
  }
}

export default BrandingContext
