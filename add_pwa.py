#!/usr/bin/env python3
"""
Add PWA manifest to O.D.I.N. for mobile "Add to Homescreen" support.
Run on server: python3 add_pwa.py

Creates manifest.json and updates index.html with the link.
You already have sw.js for push notifications — this completes the PWA.
"""

import os
import json

FRONTEND_PUBLIC = "/opt/printfarm-scheduler/frontend/public"
FRONTEND_ROOT = "/opt/printfarm-scheduler/frontend"

changes = 0

# ============================================================
# 1. Create manifest.json
# ============================================================

manifest = {
    "name": "O.D.I.N. — Orchestrated Dispatch & Inventory Network",
    "short_name": "O.D.I.N.",
    "description": "Self-hosted 3D print farm management system",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#0a0c10",
    "theme_color": "#d97706",
    "orientation": "any",
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
    ]
}

manifest_path = os.path.join(FRONTEND_PUBLIC, "manifest.json")
with open(manifest_path, "w") as f:
    json.dump(manifest, f, indent=2)
changes += 1
print("✓ Created manifest.json")

# ============================================================
# 2. Create SVG icons (simple O.D.I.N. rune icon)
# ============================================================

icon_svg = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <rect width="512" height="512" rx="96" fill="#0a0c10"/>
  <rect x="16" y="16" width="480" height="480" rx="80" fill="none" stroke="#d97706" stroke-width="4" opacity="0.3"/>
  <text x="256" y="300" text-anchor="middle" font-family="monospace" font-weight="700" font-size="220" fill="#f59e0b">ᛟ</text>
  <text x="256" y="420" text-anchor="middle" font-family="monospace" font-weight="700" font-size="64" fill="#d97706" letter-spacing="12">ODIN</text>
</svg>'''

for size in ["192", "512"]:
    icon_path = os.path.join(FRONTEND_PUBLIC, f"odin-icon-{size}.svg")
    with open(icon_path, "w") as f:
        f.write(icon_svg)
    print(f"✓ Created odin-icon-{size}.svg")
    changes += 1

# ============================================================
# 3. Update index.html to include manifest link + theme-color
# ============================================================

index_html_path = os.path.join(FRONTEND_ROOT, "index.html")

with open(index_html_path, "r") as f:
    html = f.read()

if 'manifest.json' not in html:
    # Add manifest link and theme-color meta in <head>
    head_additions = '''    <link rel="manifest" href="/manifest.json">
    <meta name="theme-color" content="#d97706">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <link rel="apple-touch-icon" href="/odin-icon-192.svg">'''
    
    # Insert before </head>
    html = html.replace("</head>", head_additions + "\n  </head>")
    
    with open(index_html_path, "w") as f:
        f.write(html)
    changes += 1
    print("✓ Updated index.html with manifest link and meta tags")
else:
    print("· manifest.json already linked in index.html")

# ============================================================
# 4. Update sw.js to handle PWA install (add fetch handler)
# ============================================================

sw_path = os.path.join(FRONTEND_PUBLIC, "sw.js")

if os.path.exists(sw_path):
    with open(sw_path, "r") as f:
        sw_content = f.read()
    
    if "fetch" not in sw_content:
        # Add minimal fetch handler for offline PWA support
        sw_addition = '''

// PWA fetch handler — serve app shell from network, fallback gracefully
self.addEventListener('fetch', (event) => {
  // Let all requests pass through to network (no offline cache for now)
  // This satisfies PWA installability requirements
  event.respondWith(fetch(event.request));
});
'''
        sw_content += sw_addition
        with open(sw_path, "w") as f:
            f.write(sw_content)
        changes += 1
        print("✓ Added fetch handler to sw.js")
    else:
        print("· sw.js already has fetch handler")
else:
    print("✗ sw.js not found in public/")

print(f"\n✅ PWA setup complete ({changes} changes)")
