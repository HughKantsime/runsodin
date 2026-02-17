import { useState, useEffect, useRef } from "react"
import { Palette, Upload, RotateCcw, Eye, Save, Image, Type, Paintbrush, Monitor, PanelLeft } from "lucide-react"
import ConfirmModal from '../components/ConfirmModal'

const API_BASE = '/api'

// System fonts only - no external CDN calls, works airgapped/ITAR
// Each entry is [display name, font-family stack]
const DISPLAY_FONTS = [
  { name: "System Default", stack: "system-ui, -apple-system, sans-serif" },
  { name: "Inter", stack: "Inter, system-ui, sans-serif" },
  { name: "Roboto", stack: "Roboto, system-ui, sans-serif" },
  { name: "Montserrat", stack: "Montserrat, system-ui, sans-serif" },
  { name: "Poppins", stack: "Poppins, system-ui, sans-serif" },
  { name: "Raleway", stack: "Raleway, system-ui, sans-serif" },
  { name: "Nunito", stack: "Nunito, system-ui, sans-serif" },
  { name: "Space Grotesk", stack: "Space Grotesk, system-ui, sans-serif" },
  { name: "Outfit", stack: "Outfit, system-ui, sans-serif" },
  { name: "Urbanist", stack: "Urbanist, system-ui, sans-serif" },
  { name: "Oswald", stack: "Oswald, system-ui, sans-serif" },
  { name: "IBM Plex Sans", stack: "IBM Plex Sans, system-ui, sans-serif" },
  { name: "Helvetica", stack: "Helvetica Neue, Helvetica, Arial, sans-serif" },
  { name: "Arial", stack: "Arial, Helvetica, sans-serif" },
  { name: "Segoe UI", stack: "Segoe UI, Tahoma, Geneva, sans-serif" },
  { name: "San Francisco", stack: "-apple-system, BlinkMacSystemFont, sans-serif" },
  { name: "Trebuchet MS", stack: "Trebuchet MS, Lucida Grande, sans-serif" },
  { name: "Verdana", stack: "Verdana, Geneva, sans-serif" },
  { name: "Tahoma", stack: "Tahoma, Geneva, Verdana, sans-serif" },
  { name: "Lucida Grande", stack: "Lucida Grande, Lucida Sans, sans-serif" },
  { name: "Impact", stack: "Impact, Haettenschweiler, sans-serif" },
  { name: "Playfair Display", stack: "Playfair Display, Georgia, serif" },
  { name: "Lora", stack: "Lora, Georgia, serif" },
  { name: "Georgia", stack: "Georgia, Cambria, serif" },
  { name: "Palatino", stack: "Palatino Linotype, Palatino, Book Antiqua, serif" },
  { name: "Times New Roman", stack: "Times New Roman, Times, serif" },
  { name: "Garamond", stack: "Garamond, Baskerville, serif" },
  { name: "IBM Plex Mono", stack: "IBM Plex Mono, monospace" },
]

const BODY_FONTS = [
  { name: "System Default", stack: "system-ui, -apple-system, sans-serif" },
  { name: "Inter", stack: "Inter, system-ui, sans-serif" },
  { name: "Roboto", stack: "Roboto, system-ui, sans-serif" },
  { name: "Open Sans", stack: "Open Sans, system-ui, sans-serif" },
  { name: "Lato", stack: "Lato, system-ui, sans-serif" },
  { name: "Source Sans 3", stack: "Source Sans 3, system-ui, sans-serif" },
  { name: "DM Sans", stack: "DM Sans, system-ui, sans-serif" },
  { name: "Nunito", stack: "Nunito, system-ui, sans-serif" },
  { name: "Poppins", stack: "Poppins, system-ui, sans-serif" },
  { name: "IBM Plex Sans", stack: "IBM Plex Sans, system-ui, sans-serif" },
  { name: "Helvetica", stack: "Helvetica Neue, Helvetica, Arial, sans-serif" },
  { name: "Arial", stack: "Arial, Helvetica, sans-serif" },
  { name: "Segoe UI", stack: "Segoe UI, Tahoma, Geneva, sans-serif" },
  { name: "San Francisco", stack: "-apple-system, BlinkMacSystemFont, sans-serif" },
  { name: "Verdana", stack: "Verdana, Geneva, sans-serif" },
  { name: "Tahoma", stack: "Tahoma, Geneva, Verdana, sans-serif" },
  { name: "Trebuchet MS", stack: "Trebuchet MS, Lucida Grande, sans-serif" },
  { name: "Lucida Grande", stack: "Lucida Grande, Lucida Sans, sans-serif" },
  { name: "Merriweather", stack: "Merriweather, Georgia, serif" },
  { name: "Crimson Text", stack: "Crimson Text, Georgia, serif" },
  { name: "Source Serif 4", stack: "Source Serif 4, Georgia, serif" },
  { name: "Georgia", stack: "Georgia, Cambria, serif" },
  { name: "Palatino", stack: "Palatino Linotype, Palatino, Book Antiqua, serif" },
  { name: "Times New Roman", stack: "Times New Roman, Times, serif" },
]

const MONO_FONTS = [
  { name: "System Mono", stack: "ui-monospace, monospace" },
  { name: "JetBrains Mono", stack: "JetBrains Mono, monospace" },
  { name: "Fira Code", stack: "Fira Code, monospace" },
  { name: "Source Code Pro", stack: "Source Code Pro, monospace" },
  { name: "Space Mono", stack: "Space Mono, monospace" },
  { name: "Inconsolata", stack: "Inconsolata, monospace" },
  { name: "IBM Plex Mono", stack: "IBM Plex Mono, monospace" },
  { name: "Menlo", stack: "Menlo, Monaco, monospace" },
  { name: "Consolas", stack: "Consolas, Courier New, monospace" },
  { name: "Courier New", stack: "Courier New, Courier, monospace" },
  { name: "SF Mono", stack: "SFMono-Regular, Menlo, monospace" },
  { name: "Lucida Console", stack: "Lucida Console, Monaco, monospace" },
]

const DEFAULTS = {
  app_name: "O.D.I.N.",
  app_subtitle: "Scheduler",
  primary_color: "#d97706",
  accent_color: "#f59e0b",
  sidebar_bg: "#0a0c10",
  sidebar_border: "#1a2030",
  sidebar_text: "#8b95a8",
  sidebar_active_bg: "#1a203040",
  sidebar_active_text: "#f59e0b",
  content_bg: "#0a0c10",
  card_bg: "#0f1218",
  card_border: "#1a2030",
  text_primary: "#e8ecf2",
  text_secondary: "#8b95a8",
  text_muted: "#364155",
  input_bg: "#1a2030",
  input_border: "#252d3d",
  font_display: "system-ui, -apple-system, sans-serif",
  font_body: "system-ui, -apple-system, sans-serif",
  font_mono: "ui-monospace, monospace",
  footer_text: "System Online",
  support_url: "",
}

function getAuthHeaders(contentType = 'application/json') {
  const headers = {}
  if (contentType) headers['Content-Type'] = contentType
  const token = localStorage.getItem('token')
  if (token) headers['Authorization'] = `Bearer ${token}`
  const apiKey = import.meta.env.VITE_API_KEY
  if (apiKey) headers['X-API-Key'] = apiKey
  return headers
}

// Google Fonts URL for branding font picker — loaded on-demand
const BRANDING_FONTS_URL = 'https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Roboto:wght@300;400;500;700&family=Open+Sans:wght@300;400;600;700&family=Lato:wght@300;400;700&family=Montserrat:wght@300;400;500;600;700&family=Poppins:wght@300;400;500;600;700&family=Raleway:wght@300;400;500;600;700&family=Nunito:wght@300;400;600;700&family=Source+Sans+3:wght@300;400;600;700&family=DM+Sans:wght@400;500;700&family=Space+Grotesk:wght@300;400;500;600;700&family=Outfit:wght@300;400;500;600;700&family=Urbanist:wght@300;400;500;600;700&family=Oswald:wght@300;400;500;600;700&family=Playfair+Display:wght@400;500;600;700&family=Merriweather:wght@300;400;700&family=Lora:wght@400;500;600;700&family=Crimson+Text:wght@400;600;700&family=Source+Serif+4:wght@300;400;600;700&family=JetBrains+Mono:wght@400;500;600;700&family=Fira+Code:wght@400;500;600;700&family=Source+Code+Pro:wght@400;500;600;700&family=Space+Mono:wght@400;700&family=Inconsolata:wght@400;500;600;700&display=swap'

export default function Branding() {
  const [branding, setBranding] = useState(null)
  const [draft, setDraft] = useState(null)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState(null)
  const [activeTab, setActiveTab] = useState("identity")
  const [showResetConfirm, setShowResetConfirm] = useState(false)
  const logoInputRef = useRef(null)
  const faviconInputRef = useRef(null)

  // Lazy-load Google Fonts when Branding page mounts
  useEffect(() => {
    const id = 'branding-google-fonts'
    if (!document.getElementById(id)) {
      const link = document.createElement('link')
      link.id = id
      link.rel = 'stylesheet'
      link.href = BRANDING_FONTS_URL
      document.head.appendChild(link)
    }
  }, [])

  useEffect(() => {
    fetch(`${API_BASE}/branding`)
      .then(res => res.json())
      .then(data => {
        setBranding(data)
        setDraft({ ...DEFAULTS, ...data })
      })
      .catch(() => setError("Failed to load branding config"))
  }, [])

  const updateDraft = (field, value) => {
    setDraft(prev => ({ ...prev, [field]: value }))
    setSaved(false)
  }

  const handleSave = async () => {
    setSaving(true)
    setError(null)
    try {
      const res = await fetch(`${API_BASE}/branding`, {
        method: 'PUT',
        headers: getAuthHeaders(),
        body: JSON.stringify(draft)
      })
      if (!res.ok) throw new Error("Failed to save")
      const data = await res.json()
      setBranding(data)
      setDraft({ ...DEFAULTS, ...data })
      setSaved(true)
      applyLiveCSS(data)
      setTimeout(() => setSaved(false), 3000)
    } catch (err) {
      setError(err.message)
    } finally {
      setSaving(false)
    }
  }

  const handleReset = () => {
    setShowResetConfirm(true)
  }

  const doReset = () => {
    setDraft({ ...DEFAULTS, logo_url: draft?.logo_url, favicon_url: draft?.favicon_url })
    setSaved(false)
    setShowResetConfirm(false)
  }

  const handleRemoveFavicon = async () => {
    try {
      const res = await fetch(`${API_BASE}/branding/favicon`, {
        method: 'DELETE',
        headers: getAuthHeaders(null),
      })
      if (!res.ok) throw new Error("Remove failed")
      setBranding(prev => ({ ...prev, favicon_url: null }))
      setDraft(prev => ({ ...prev, favicon_url: null }))
    } catch (err) {
      setError(err.message)
    }
  }

  const handleFileUpload = async (endpoint, e) => {
    const file = e.target.files[0]
    if (!file) return
    const formData = new FormData()
    formData.append("file", file)
    try {
      const res = await fetch(`${API_BASE}/branding/${endpoint}`, {
        method: 'POST',
        headers: getAuthHeaders(null),
        body: formData
      })
      if (!res.ok) throw new Error("Upload failed")
      const data = await res.json()
      setBranding(prev => ({ ...prev, ...data }))
      setDraft(prev => ({ ...prev, ...data }))
    } catch (err) {
      setError(err.message)
    }
  }

  const handleRemoveLogo = async () => {
    try {
      const res = await fetch(`${API_BASE}/branding/logo`, {
        method: 'DELETE',
        headers: getAuthHeaders(null),
      })
      if (!res.ok) throw new Error("Remove failed")
      setBranding(prev => ({ ...prev, logo_url: null }))
      setDraft(prev => ({ ...prev, logo_url: null }))
    } catch (err) {
      setError(err.message)
    }
  }

  if (!draft) {
    return (
      <div className="p-6 md:p-8">
        <div className="text-farm-400">Loading branding configuration...</div>
      </div>
    )
  }

  const hasChanges = JSON.stringify(draft) !== JSON.stringify(branding)

  const tabs = [
    { id: "identity", label: "Identity", icon: Paintbrush },
    { id: "sidebar", label: "Sidebar", icon: PanelLeft },
    { id: "content", label: "Content", icon: Monitor },
    { id: "fonts", label: "Typography", icon: Type },
  ]

  return (
    <div className="p-6 md:p-8 max-w-7xl mx-auto">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between mb-8 gap-4">
        <div>
          <div className="flex items-center gap-3">
            <Palette className="text-print-400" size={24} />
            <div>
              <h1 className="text-xl md:text-2xl font-display font-bold">White-Label Branding</h1>
              <p className="text-farm-500 text-sm mt-1">Customize every aspect of your deployment's look and feel</p>
            </div>
          </div>
        </div>
        <div className="flex items-center gap-3">
          <button
            onClick={handleReset}
            className="flex items-center gap-2 px-4 py-2 rounded-lg bg-farm-800 text-farm-300 hover:bg-farm-700 transition-colors text-sm"
          >
            <RotateCcw size={16} />
            Reset
          </button>
          <button
            onClick={handleSave}
            disabled={saving || !hasChanges}
            className="flex items-center gap-2 px-5 py-2 rounded-lg text-sm font-medium transition-colors"
            style={
              saved
                ? { backgroundColor: '#16a34a', color: '#fff' }
                : hasChanges
                ? { backgroundColor: 'var(--brand-primary)', color: '#fff' }
                : { backgroundColor: 'var(--brand-input-bg)', color: 'var(--brand-text-muted)', cursor: 'not-allowed' }
            }
          >
            <Save size={16} />
            {saving ? "Saving..." : saved ? "Saved!" : "Save Changes"}
          </button>
        </div>
      </div>

      {error && (
        <div className="mb-6 p-4 bg-red-900/30 border border-red-700 rounded-lg text-red-400 text-sm">
          {error}
        </div>
      )}

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-8">
        {/* Settings Column */}
        <div className="xl:col-span-2 space-y-6">
          {/* Tabs */}
          <div className="flex gap-1 bg-farm-900 rounded-lg p-1 border border-farm-800">
            {tabs.map(tab => (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 rounded-lg text-sm font-medium transition-colors"
                style={
                  activeTab === tab.id
                    ? { backgroundColor: 'var(--brand-sidebar-active-bg)', color: 'var(--brand-accent)' }
                    : { color: 'var(--brand-text-secondary)' }
                }
              >
                <tab.icon size={16} />
                <span className="hidden sm:inline">{tab.label}</span>
              </button>
            ))}
          </div>

          {/* Identity Tab */}
          {activeTab === "identity" && (
            <div className="space-y-6">
              <Section title="App Identity">
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  <TextInput label="App Name" value={draft.app_name} onChange={v => updateDraft("app_name", v)} placeholder="O.D.I.N." />
                  <TextInput label="Subtitle" value={draft.app_subtitle} onChange={v => updateDraft("app_subtitle", v)} placeholder="Scheduler" />
                  <TextInput label="Footer Text" value={draft.footer_text} onChange={v => updateDraft("footer_text", v)} placeholder="System Online" />
                  <TextInput label="Support URL" value={draft.support_url || ""} onChange={v => updateDraft("support_url", v)} placeholder="https://support.example.com" type="url" />
                </div>
              </Section>

              <Section title="Brand Colors">
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                  <ColorPicker label="Primary" desc="Buttons, active states" value={draft.primary_color} onChange={v => updateDraft("primary_color", v)} />
                  <ColorPicker label="Accent" desc="Highlights, hover states" value={draft.accent_color} onChange={v => updateDraft("accent_color", v)} />
                </div>
              </Section>

              <Section title="Logo & Favicon">
                <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                  <FileUpload
                    label="Brand Logo"
                    preview={draft.logo_url}
                    previewClass="max-h-16"
                    accept="image/png,image/jpeg,image/svg+xml,image/webp"
                    hint="PNG, JPEG, SVG, or WebP"
                    inputRef={logoInputRef}
                    onUpload={e => handleFileUpload("logo", e)}
                    onRemove={handleRemoveLogo}
                  />
                  <FileUpload
                    label="Favicon"
                    preview={draft.favicon_url}
                    previewClass="w-8 h-8"
                    accept="image/png,image/x-icon,image/svg+xml,image/webp"
                    hint="PNG, ICO, SVG, or WebP"
                    inputRef={faviconInputRef}
                    onUpload={e => handleFileUpload("favicon", e)}
                    onRemove={handleRemoveFavicon}
                  />
                </div>
              </Section>
            </div>
          )}

          {/* Sidebar Tab */}
          {activeTab === "sidebar" && (
            <Section title="Sidebar Colors">
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <ColorPicker label="Background" desc="Sidebar background" value={draft.sidebar_bg} onChange={v => updateDraft("sidebar_bg", v)} />
                <ColorPicker label="Border" desc="Dividers and separators" value={draft.sidebar_border} onChange={v => updateDraft("sidebar_border", v)} />
                <ColorPicker label="Text" desc="Inactive nav text" value={draft.sidebar_text} onChange={v => updateDraft("sidebar_text", v)} />
                <ColorPicker label="Active Background" desc="Selected nav item bg" value={draft.sidebar_active_bg} onChange={v => updateDraft("sidebar_active_bg", v)} />
                <ColorPicker label="Active Text" desc="Selected nav item text" value={draft.sidebar_active_text} onChange={v => updateDraft("sidebar_active_text", v)} />
              </div>
            </Section>
          )}

          {/* Content Tab */}
          {activeTab === "content" && (
            <div className="space-y-6">
              <Section title="Page Colors">
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                  <ColorPicker label="Content Background" desc="Main page background" value={draft.content_bg} onChange={v => updateDraft("content_bg", v)} />
                  <ColorPicker label="Card Background" desc="Cards and panels" value={draft.card_bg} onChange={v => updateDraft("card_bg", v)} />
                  <ColorPicker label="Card Border" desc="Card borders and dividers" value={draft.card_border} onChange={v => updateDraft("card_border", v)} />
                </div>
              </Section>

              <Section title="Text Colors">
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                  <ColorPicker label="Primary Text" desc="Headings and body text" value={draft.text_primary} onChange={v => updateDraft("text_primary", v)} />
                  <ColorPicker label="Secondary Text" desc="Labels and descriptions" value={draft.text_secondary} onChange={v => updateDraft("text_secondary", v)} />
                  <ColorPicker label="Muted Text" desc="Hints and placeholders" value={draft.text_muted} onChange={v => updateDraft("text_muted", v)} />
                </div>
              </Section>

              <Section title="Input Colors">
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                  <ColorPicker label="Input Background" desc="Text fields and selects" value={draft.input_bg} onChange={v => updateDraft("input_bg", v)} />
                  <ColorPicker label="Input Border" desc="Field borders" value={draft.input_border} onChange={v => updateDraft("input_border", v)} />
                </div>
              </Section>
            </div>
          )}

          {/* Typography Tab */}
          {activeTab === "fonts" && (
            <Section title="Font Families">
              <div className="space-y-4">
                <FontSelector
                  label="Display Font"
                  desc="Used for headings, the app name, and large text"
                  value={draft.font_display}
                  options={DISPLAY_FONTS}
                  onChange={v => updateDraft("font_display", v)}
                />
                <FontSelector
                  label="Body Font"
                  desc="Used for all body text, labels, and descriptions"
                  value={draft.font_body}
                  options={BODY_FONTS}
                  onChange={v => updateDraft("font_body", v)}
                />
                <FontSelector
                  label="Monospace Font"
                  desc="Used for code, IDs, technical values"
                  value={draft.font_mono}
                  options={MONO_FONTS}
                  onChange={v => updateDraft("font_mono", v)}
                />
              </div>

              {/* Font preview */}
              <div className="mt-6 bg-farm-800 rounded-lg p-5 space-y-4">
                <p className="text-xs text-farm-500 uppercase tracking-wider mb-3">Preview</p>
                <div style={{ fontFamily: draft.font_display }}>
                  <p className="text-2xl font-bold" style={{ color: draft.text_primary }}>
                    The quick brown fox jumps
                  </p>
                  <p className="text-lg font-semibold mt-1" style={{ color: draft.text_secondary }}>
                    Display font
                  </p>
                </div>
                <div style={{ fontFamily: draft.font_body }}>
                  <p className="text-sm" style={{ color: draft.text_primary }}>
                    Lorem ipsum dolor sit amet, consectetur adipiscing elit. Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam.
                  </p>
                  <p className="text-xs mt-1" style={{ color: draft.text_muted }}>
                    Body font
                  </p>
                </div>
                <div style={{ fontFamily: draft.font_mono }}>
                  <p className="text-sm" style={{ color: draft.accent_color }}>
                    const printer = await connect("192.168.1.100")
                  </p>
                  <p className="text-xs mt-1" style={{ color: draft.text_muted }}>
                    Mono font
                  </p>
                </div>
              </div>
            </Section>
          )}
        </div>

        {/* Live Preview Column */}
        <div className="space-y-6">
          <div className="bg-farm-900 rounded-lg border border-farm-800 p-6 sticky top-8">
            <h2 className="text-lg font-semibold text-farm-100 mb-4 flex items-center gap-2">
              <Eye size={18} style={{ color: 'var(--brand-accent)' }} />
              Live Preview
            </h2>

            {/* Mini sidebar preview */}
            <div className="rounded-lg overflow-hidden border" style={{ borderColor: draft.sidebar_border }}>
              {/* Header */}
              <div
                className="p-4 flex items-center gap-3"
                style={{
                  backgroundColor: draft.sidebar_bg,
                  borderBottom: `1px solid ${draft.sidebar_border}`,
                  fontFamily: draft.font_display
                }}
              >
                {draft.logo_url ? (
                  <img src={draft.logo_url} alt="Custom logo" className="h-6 object-contain" />
                ) : (
                  <div className="w-6 h-6 rounded-lg" style={{ backgroundColor: draft.primary_color }} />
                )}
                <div>
                  <div className="text-sm font-bold" style={{ color: draft.text_primary }}>{draft.app_name}</div>
                  <div className="text-[10px]" style={{ color: draft.text_muted }}>{draft.app_subtitle}</div>
                </div>
              </div>

              {/* Nav items */}
              <div className="p-2 space-y-1" style={{
                backgroundColor: draft.sidebar_bg,
                fontFamily: draft.font_body
              }}>
                <div
                  className="flex items-center gap-2 px-3 py-2 rounded-lg text-xs"
                  style={{ backgroundColor: draft.sidebar_active_bg }}
                >
                  <div className="w-3 h-3 rounded-sm" style={{ backgroundColor: draft.sidebar_active_text }} />
                  <span style={{ color: draft.sidebar_active_text }}>Dashboard</span>
                </div>
                {["Printers", "Jobs", "Spools"].map(item => (
                  <div key={item} className="flex items-center gap-2 px-3 py-2 rounded-lg text-xs" style={{ color: draft.sidebar_text }}>
                    <div className="w-3 h-3 rounded-sm" style={{ backgroundColor: draft.sidebar_text, opacity: 0.5 }} />
                    <span>{item}</span>
                  </div>
                ))}
              </div>

              {/* Footer */}
              <div
                className="px-4 py-3 text-[10px] flex items-center gap-2"
                style={{
                  backgroundColor: draft.sidebar_bg,
                  borderTop: `1px solid ${draft.sidebar_border}`,
                  color: draft.text_muted,
                }}
              >
                <div className="w-2 h-2 rounded-full bg-green-500" />
                {draft.footer_text}
              </div>
            </div>

            {/* Content area preview */}
            <div className="mt-4">
              <p className="text-xs text-farm-500 mb-2">Content Area</p>
              <div className="rounded-lg p-3 space-y-2" style={{
                backgroundColor: draft.content_bg,
                border: `1px solid ${draft.card_border}`,
                fontFamily: draft.font_body
              }}>
                <div className="rounded-lg p-3" style={{ backgroundColor: draft.card_bg, border: `1px solid ${draft.card_border}` }}>
                  <p className="text-xs font-semibold" style={{ color: draft.text_primary, fontFamily: draft.font_display }}>
                    Active Printers
                  </p>
                  <p className="text-[10px] mt-0.5" style={{ color: draft.text_secondary }}>3 of 5 online</p>
                  <div className="flex gap-2 mt-2">
                    <div className="flex-1 h-6 rounded-lg text-[10px] flex items-center justify-center font-medium text-white" style={{ backgroundColor: draft.primary_color }}>
                      Start Job
                    </div>
                    <div className="flex-1 h-6 rounded-lg text-[10px] flex items-center justify-center" style={{ backgroundColor: draft.input_bg, border: `1px solid ${draft.input_border}`, color: draft.text_secondary }}>
                      Settings
                    </div>
                  </div>
                </div>
                <div className="rounded-lg px-3 py-2 text-[10px]" style={{ backgroundColor: draft.input_bg, border: `1px solid ${draft.input_border}`, color: draft.text_muted, fontFamily: `'${draft.font_mono}', monospace` }}>
                  printer.status = "idle"
                </div>
              </div>
            </div>

            {/* Login preview */}
            <div className="mt-4">
              <p className="text-xs text-farm-500 mb-2">Login Screen</p>
              <div className="rounded-lg p-4" style={{ backgroundColor: draft.content_bg, border: `1px solid ${draft.card_border}` }}>
                <div className="text-center mb-3" style={{ fontFamily: draft.font_display }}>
                  {draft.logo_url ? (
                    <img src={draft.logo_url} alt="Custom logo" className="h-8 mx-auto mb-1" />
                  ) : (
                    <div className="text-lg font-bold" style={{ color: draft.accent_color }}>
                      {draft.app_name.toUpperCase()}
                    </div>
                  )}
                  <div className="text-[10px]" style={{ color: draft.text_muted }}>{draft.app_subtitle}</div>
                </div>
                <div className="space-y-2">
                  <div className="h-7 rounded-lg" style={{ backgroundColor: draft.input_bg, border: `1px solid ${draft.input_border}` }} />
                  <div className="h-7 rounded-lg" style={{ backgroundColor: draft.input_bg, border: `1px solid ${draft.input_border}` }} />
                  <div className="h-7 rounded-lg text-[10px] text-white font-medium flex items-center justify-center" style={{ backgroundColor: draft.primary_color }}>
                    Sign In
                  </div>
                </div>
              </div>
            </div>

            {/* Palette swatch */}
            <div className="mt-4">
              <p className="text-xs text-farm-500 mb-2">Active Palette</p>
              <div className="grid grid-cols-4 gap-1.5">
                {[
                  { c: draft.primary_color, l: "Primary" },
                  { c: draft.accent_color, l: "Accent" },
                  { c: draft.sidebar_bg, l: "Sidebar" },
                  { c: draft.content_bg, l: "Content" },
                  { c: draft.card_bg, l: "Card" },
                  { c: draft.input_bg, l: "Input" },
                  { c: draft.text_primary, l: "Text" },
                  { c: draft.text_muted, l: "Muted" },
                ].map(({ c, l }) => (
                  <div key={l} className="text-center">
                    <div className="h-6 rounded-lg border border-farm-700" style={{ backgroundColor: c }} />
                    <p className="text-[8px] text-farm-500 mt-0.5">{l}</p>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      </div>

      <ConfirmModal
        open={showResetConfirm}
        onConfirm={doReset}
        onCancel={() => setShowResetConfirm(false)}
        title="Reset Branding"
        message="Reset all colors and text to factory defaults? Your logo and favicon will be kept."
        confirmText="Reset to Defaults"
        confirmVariant="danger"
      />
    </div>
  )
}

// Apply CSS vars live (called after save)
// Mirrors BrandingContext.applyBrandingCSS: in light mode, only apply
// theme-neutral vars (primary, accent, fonts) and remove surface overrides
// so the html.light CSS block in index.css takes effect.
function applyLiveCSS(b) {
  const root = document.documentElement
  const isLight = root.classList.contains("light")

  // Darken a hex color until it meets WCAG contrast against white
  const ensureContrast = (hex, minRatio = 4.5) => {
    const lum = (h) => {
      const r = parseInt(h.slice(1,3),16)/255, g = parseInt(h.slice(3,5),16)/255, bl = parseInt(h.slice(5,7),16)/255
      const toL = c => c <= 0.03928 ? c/12.92 : Math.pow((c+0.055)/1.055, 2.4)
      return 0.2126*toL(r) + 0.7152*toL(g) + 0.0722*toL(bl)
    }
    const cr = (a, b) => { const l1 = Math.max(lum(a),lum(b)), l2 = Math.min(lum(a),lum(b)); return (l1+0.05)/(l2+0.05) }
    let c = hex
    for (let i = 0; i < 20; i++) {
      if (cr(c, "#ffffff") >= minRatio) return c
      const r = Math.max(0,parseInt(c.slice(1,3),16)-15), g = Math.max(0,parseInt(c.slice(3,5),16)-15), bl = Math.max(0,parseInt(c.slice(5,7),16)-15)
      c = `#${r.toString(16).padStart(2,'0')}${g.toString(16).padStart(2,'0')}${bl.toString(16).padStart(2,'0')}`
    }
    return c
  }

  // Primary / accent — always apply (darken for contrast in light mode)
  const primary = isLight ? ensureContrast(b.primary_color) : b.primary_color
  root.style.setProperty("--brand-primary", primary)
  root.style.setProperty("--brand-accent", b.accent_color)

  // Fonts — always apply
  root.style.setProperty("--brand-font-display", b.font_display)
  root.style.setProperty("--brand-font-body", b.font_body)
  root.style.setProperty("--brand-font-mono", b.font_mono)

  if (isLight) {
    // Remove surface overrides so html.light CSS block takes effect
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

  document.title = b.app_subtitle ? `${b.app_name} ${b.app_subtitle}` : b.app_name
}


// ============== Sub-components ==============

function Section({ title, children }) {
  return (
    <div className="bg-farm-900 rounded-lg border border-farm-800 p-6">
      <h2 className="text-lg font-semibold text-farm-100 mb-4">{title}</h2>
      {children}
    </div>
  )
}

function TextInput({ label, value, onChange, placeholder, type = "text" }) {
  return (
    <div>
      <label className="block text-sm text-farm-400 mb-2">{label}</label>
      <input
        type={type}
        value={value}
        onChange={e => onChange(e.target.value)}
        className="w-full bg-farm-800 border border-farm-700 rounded-lg px-4 py-2.5 text-farm-100 focus:outline-none focus:border-farm-500 transition-colors"
        placeholder={placeholder}
      />
    </div>
  )
}

function ColorPicker({ label, desc, value, onChange }) {
  return (
    <div className="flex items-center gap-4 bg-farm-800 rounded-lg p-3">
      <input
        type="color"
        value={value}
        onChange={e => onChange(e.target.value)}
        className="w-10 h-10 rounded-lg cursor-pointer border-2 border-farm-600 bg-transparent flex-shrink-0"
        style={{ padding: 0 }}
      />
      <div className="flex-1 min-w-0">
        <div className="text-sm text-farm-200 font-medium">{label}</div>
        <div className="text-xs text-farm-500">{desc}</div>
      </div>
      <input
        type="text"
        value={value}
        onChange={e => onChange(e.target.value)}
        className="w-20 bg-farm-900 border border-farm-700 rounded-lg px-2 py-1 text-xs text-farm-300 font-mono text-center"
        maxLength={7}
      />
    </div>
  )
}

function FontSelector({ label, desc, value, options, onChange }) {
  // Find matching font name for display
  const currentFont = options.find(f => f.stack === value)
  const displayName = currentFont ? currentFont.name : "Custom"

  return (
    <div className="bg-farm-800 rounded-lg p-4">
      <div className="flex items-start justify-between mb-3">
        <div>
          <div className="text-sm text-farm-200 font-medium">{label}</div>
          <div className="text-xs text-farm-500">{desc}</div>
        </div>
      </div>
      <select
        value={value}
        onChange={e => onChange(e.target.value)}
        className="w-full bg-farm-900 border border-farm-700 rounded-lg px-4 py-2.5 text-farm-100 focus:outline-none focus:border-farm-500 transition-colors appearance-none cursor-pointer"
        style={{ fontFamily: value }}
      >
        {options.map(font => (
          <option key={font.name} value={font.stack} style={{ fontFamily: font.stack }}>
            {font.name}
          </option>
        ))}
      </select>
      <div className="mt-2 text-lg" style={{ fontFamily: value, color: "var(--brand-text-primary)" }}>
        Aa Bb Cc 0123456789
      </div>
    </div>
  )
}

function FileUpload({ label, preview, previewClass, accept, hint, inputRef, onUpload, onRemove }) {
  return (
    <div>
      <label className="block text-sm text-farm-400 mb-2">{label}</label>
      <div className="border-2 border-dashed border-farm-700 rounded-lg p-6 text-center hover:border-farm-500 transition-colors">
        {preview ? (
          <div className="space-y-3">
            <img src={preview} alt={label} className={`mx-auto object-contain ${previewClass}`} />
            <div className="flex items-center justify-center gap-2">
              <button onClick={() => inputRef.current?.click()} className="text-xs" style={{ color: 'var(--brand-accent)' }}>Replace</button>
              {onRemove && (
                <>
                  <span className="text-farm-600">|</span>
                  <button onClick={onRemove} className="text-xs text-red-400 hover:text-red-300">Remove</button>
                </>
              )}
            </div>
          </div>
        ) : (
          <button onClick={() => inputRef.current?.click()} className="space-y-2 w-full">
            <Image size={32} className="mx-auto text-farm-500" />
            <p className="text-sm text-farm-400">Click to upload</p>
            <p className="text-xs text-farm-600">{hint}</p>
          </button>
        )}
        <input ref={inputRef} type="file" accept={accept} onChange={onUpload} className="hidden" />
      </div>
    </div>
  )
}
