#!/usr/bin/env python3
"""
O.D.I.N. License Gating — Route Patch

Wraps Pro-only routes with <ProGate> so direct URL access
shows upgrade prompt instead of the page.

Run: python3 patch_route_gating.py
Then: cd /opt/printfarm-scheduler/frontend && npm run build
"""

import os

APP = '/opt/printfarm-scheduler/frontend/src/App.jsx'
steps = 0
errors = 0

def patch(old, new, label):
    global steps, errors
    content = open(APP).read()
    if old not in content:
        if new in content:
            print(f"  ⚠️  {label} — already applied")
            return
        print(f"  ❌ {label} — target not found")
        errors += 1
        return
    content = content.replace(old, new, 1)
    open(APP, 'w').write(content)
    steps += 1
    print(f"  ✅ {label}")

print("=" * 60)
print("  O.D.I.N. Route Gating Patch")
print("=" * 60)
print()

# Orders
patch(
    '<Route path="/orders" element={<Orders />} />',
    '<Route path="/orders" element={<ProGate feature="orders"><Orders /></ProGate>} />',
    "Gate /orders route"
)

# Products
patch(
    '<Route path="/products" element={<Products />} />',
    '<Route path="/products" element={<ProGate feature="products"><Products /></ProGate>} />',
    "Gate /products route"
)

# Analytics
patch(
    '<Route path="/analytics" element={<Analytics />} />',
    '<Route path="/analytics" element={<ProGate feature="analytics"><Analytics /></ProGate>} />',
    "Gate /analytics route"
)

# Maintenance
patch(
    '<Route path="/maintenance" element={<Maintenance />} />',
    '<Route path="/maintenance" element={<ProGate feature="maintenance"><Maintenance /></ProGate>} />',
    "Gate /maintenance route"
)

print()
print("=" * 60)
print(f"  Done! {steps} routes gated, {errors} errors")
print()
print("  Build: cd /opt/printfarm-scheduler/frontend && npm run build")
print("  Restart: systemctl restart printfarm-backend")
print("=" * 60)
