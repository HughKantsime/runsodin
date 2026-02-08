#!/usr/bin/env python3
"""
Two features:
1. PWA polish (cache name, icon paths, id field)
2. App-wide keyboard shortcuts with help modal
"""
import os

BASE = "/opt/printfarm-scheduler"
FRONTEND = f"{BASE}/frontend/src"
PUBLIC = f"{BASE}/frontend/public"

# =============================================================================
# 1. PWA POLISH
# =============================================================================

# Update manifest.json with id field
manifest = '''{
  "id": "/",
  "name": "O.D.I.N. — Orchestrated Dispatch & Inventory Network",
  "short_name": "O.D.I.N.",
  "description": "Self-hosted 3D print farm management system",
  "start_url": "/",
  "display": "standalone",
  "background_color": "#0a0c10",
  "theme_color": "#d97706",
  "orientation": "any",
  "categories": ["productivity", "utilities"],
  "icons": [
    {
      "src": "/odin-icon-192.svg",
      "sizes": "192x192",
      "type": "image/svg+xml",
      "purpose": "any maskable"
    },
    {
      "src": "/odin-icon-512.svg",
      "sizes": "512x512",
      "type": "image/svg+xml",
      "purpose": "any maskable"
    }
  ],
  "shortcuts": [
    {
      "name": "Dashboard",
      "short_name": "Dashboard",
      "url": "/",
      "icons": [{ "src": "/odin-icon-192.svg", "sizes": "192x192" }]
    },
    {
      "name": "Jobs",
      "short_name": "Jobs",
      "url": "/jobs"
    },
    {
      "name": "Upload",
      "short_name": "Upload",
      "url": "/upload"
    }
  ]
}
'''

with open(f"{PUBLIC}/manifest.json", "w") as f:
    f.write(manifest)
print("✅ Updated manifest.json (added id, categories, shortcuts)")

# Update sw.js — fix cache name and icon path
sw_path = f"{PUBLIC}/sw.js"
with open(sw_path, "r") as f:
    sw = f.read()

sw = sw.replace("const CACHE_NAME = 'printfarm-v1'", "const CACHE_NAME = 'odin-v1'")
sw = sw.replace("title: 'PrintFarm Alert'", "title: 'O.D.I.N. Alert'")
sw = sw.replace("icon: '/logo192.png'", "icon: '/odin-icon-192.svg'")
sw = sw.replace("badge: '/badge72.png'", "badge: '/odin-icon-192.svg'")
sw = sw.replace("tag: 'printfarm-alert'", "tag: 'odin-alert'")

with open(sw_path, "w") as f:
    f.write(sw)
print("✅ Updated sw.js (odin cache name, icon paths)")

# =============================================================================
# 2. APP-WIDE KEYBOARD SHORTCUTS
# =============================================================================

# Create the hook
hook_content = '''import { useState, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'

/**
 * App-wide keyboard shortcuts.
 * 
 * Navigation (press g then letter within 1s):
 *   g d → Dashboard
 *   g j → Jobs
 *   g p → Printers
 *   g c → Cameras
 *   g u → Upload
 *   g m → Models
 *   g s → Spools
 *   g a → Analytics
 *   g o → Orders
 *   g t → Timeline
 *   g l → Alerts
 *   g x → Settings
 * 
 * Actions:
 *   Cmd/Ctrl+K → Global search (handled by GlobalSearch.jsx)
 *   ?          → Show keyboard shortcut help
 *   Escape     → Close help modal
 *   f          → Control Room mode (handled by Cameras.jsx)
 */
export default function useKeyboardShortcuts() {
  const navigate = useNavigate()
  const [showHelp, setShowHelp] = useState(false)
  const [gPrefix, setGPrefix] = useState(false)

  const handleKeyDown = useCallback((e) => {
    // Don't trigger in input/textarea/select elements
    const tag = e.target.tagName
    const editable = e.target.isContentEditable
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || editable) return

    // Don't trigger with modifier keys (except for Cmd+K which GlobalSearch handles)
    if (e.metaKey || e.ctrlKey || e.altKey) return

    const key = e.key.toLowerCase()

    // ? → show help
    if (e.key === '?') {
      e.preventDefault()
      setShowHelp(prev => !prev)
      return
    }

    // Escape → close help
    if (e.key === 'Escape') {
      setShowHelp(false)
      setGPrefix(false)
      return
    }

    // g prefix for navigation
    if (key === 'g' && !gPrefix) {
      setGPrefix(true)
      setTimeout(() => setGPrefix(false), 1000)
      return
    }

    // Navigation after g prefix
    if (gPrefix) {
      setGPrefix(false)
      const routes = {
        d: '/',
        j: '/jobs',
        p: '/printers',
        c: '/cameras',
        u: '/upload',
        m: '/models',
        s: '/spools',
        a: '/analytics',
        o: '/orders',
        t: '/timeline',
        l: '/alerts',
        x: '/settings',
        k: '/calculator',
        w: '/maintenance',
      }
      if (routes[key]) {
        e.preventDefault()
        navigate(routes[key])
        return
      }
    }
  }, [gPrefix, navigate])

  useEffect(() => {
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [handleKeyDown])

  return { showHelp, setShowHelp }
}
'''

with open(f"{FRONTEND}/hooks/useKeyboardShortcuts.js", "w") as f:
    f.write(hook_content)
print("✅ Created useKeyboardShortcuts.js hook")

# Create the help modal component
modal_content = '''import { X } from 'lucide-react'

const shortcuts = [
  { category: 'Navigation (press g then letter)', items: [
    { keys: ['g', 'd'], action: 'Dashboard' },
    { keys: ['g', 'j'], action: 'Jobs' },
    { keys: ['g', 'p'], action: 'Printers' },
    { keys: ['g', 'c'], action: 'Cameras' },
    { keys: ['g', 'u'], action: 'Upload' },
    { keys: ['g', 'm'], action: 'Models' },
    { keys: ['g', 's'], action: 'Spools' },
    { keys: ['g', 'o'], action: 'Orders' },
    { keys: ['g', 'a'], action: 'Analytics' },
    { keys: ['g', 't'], action: 'Timeline' },
    { keys: ['g', 'l'], action: 'Alerts' },
    { keys: ['g', 'x'], action: 'Settings' },
  ]},
  { category: 'Actions', items: [
    { keys: ['⌘', 'K'], action: 'Global search' },
    { keys: ['?'], action: 'Show this help' },
    { keys: ['Esc'], action: 'Close modal / cancel' },
    { keys: ['f'], action: 'Control Room mode (on Cameras page)' },
  ]},
]

export default function KeyboardShortcutsModal({ onClose }) {
  return (
    <div className="fixed inset-0 z-[200] flex items-center justify-center" onClick={onClose}>
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" />
      <div
        className="relative bg-farm-950 border border-farm-700 rounded-lg shadow-2xl w-full max-w-lg mx-4 max-h-[80vh] overflow-y-auto"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-5 py-4 border-b border-farm-800">
          <h2 className="text-lg font-semibold text-farm-100">Keyboard Shortcuts</h2>
          <button onClick={onClose} className="p-1 text-farm-400 hover:text-farm-200 rounded">
            <X size={18} />
          </button>
        </div>
        <div className="px-5 py-4 space-y-5">
          {shortcuts.map(group => (
            <div key={group.category}>
              <h3 className="text-xs font-medium text-farm-400 uppercase tracking-wider mb-2">
                {group.category}
              </h3>
              <div className="space-y-1.5">
                {group.items.map(item => (
                  <div key={item.action} className="flex items-center justify-between py-1">
                    <span className="text-sm text-farm-300">{item.action}</span>
                    <div className="flex items-center gap-1">
                      {item.keys.map((key, i) => (
                        <span key={i}>
                          {i > 0 && <span className="text-farm-600 mx-0.5">+</span>}
                          <kbd className="inline-flex items-center justify-center min-w-[24px] px-1.5 py-0.5 text-xs font-mono bg-farm-800 border border-farm-600 rounded text-farm-200">
                            {key}
                          </kbd>
                        </span>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
        <div className="px-5 py-3 border-t border-farm-800 text-center">
          <span className="text-xs text-farm-500">Press <kbd className="px-1 py-0.5 text-xs bg-farm-800 border border-farm-600 rounded">?</kbd> to toggle this help</span>
        </div>
      </div>
    </div>
  )
}
'''

with open(f"{FRONTEND}/components/KeyboardShortcutsModal.jsx", "w") as f:
    f.write(modal_content)
print("✅ Created KeyboardShortcutsModal.jsx")

# Wire into App.jsx
app_path = f"{FRONTEND}/App.jsx"
with open(app_path, "r") as f:
    app = f.read()

# Add imports
if "useKeyboardShortcuts" not in app:
    app = app.replace(
        "import useWebSocket from './hooks/useWebSocket'",
        "import useWebSocket from './hooks/useWebSocket'\nimport useKeyboardShortcuts from './hooks/useKeyboardShortcuts'\nimport KeyboardShortcutsModal from './components/KeyboardShortcutsModal'"
    )

    # Add hook call after useWebSocket() in the App function
    # The App function is at line 342, useWebSocket at 343
    app = app.replace(
        "  useWebSocket()\n",
        "  useWebSocket()\n  const { showHelp, setShowHelp } = useKeyboardShortcuts()\n"
    )

    # Add the modal rendering — find the closing of App component
    # Add before the final closing </> or </div> or similar
    # Let's find where Routes are rendered and add after
    app = app.replace(
        "      </Routes>\n",
        "      </Routes>\n      {showHelp && <KeyboardShortcutsModal onClose={() => setShowHelp(false)} />}\n"
    )

    with open(app_path, "w") as f:
        f.write(app)
    print("✅ Wired keyboard shortcuts into App.jsx")
else:
    print("✓ Keyboard shortcuts already in App.jsx")

print()
print("=" * 60)
print("✅ PWA + Keyboard Shortcuts complete!")
print("=" * 60)
print("""
PWA Polish:
  - manifest.json: added id, categories, app shortcuts (Dashboard, Jobs, Upload)
  - sw.js: updated cache name to odin-v1, fixed icon paths, O.D.I.N. branding
  - App is installable on mobile and desktop via browser "Add to Home Screen"

Keyboard Shortcuts:
  - ? → Toggle shortcut help modal
  - g then d/j/p/c/u/m/s/o/a/t/l/x → Navigate to any page
  - Cmd+K → Global search (existing)
  - Esc → Close modals
  - Shortcuts disabled in input/textarea fields
  - 1 second timeout on g prefix

Deploy:
  cd /opt/printfarm-scheduler/frontend && npm run build
  systemctl restart printfarm-backend
""")
