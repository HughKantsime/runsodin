#!/usr/bin/env python3
"""
O.D.I.N. Forge Theme — Global style migration.
Run on server: python3 forge_migrate_styles.py

This script updates all JSX/CSS files to use the new Forge palette.
Changes:
  - rounded-xl → rounded (sharp industrial corners)
  - rounded-lg → rounded (in most contexts)
  - Status color consistency
  - Focus ring colors
"""

import os
import re

FRONTEND_SRC = "/opt/printfarm-scheduler/frontend/src"

# Track changes
total_files = 0
total_changes = 0

# Replacements to apply globally across all .jsx and .js files
REPLACEMENTS = [
    # ============================================================
    # Sharpen corners — industrial look
    # ============================================================
    # Cards and containers: rounded-xl → rounded
    ("rounded-xl", "rounded"),
    # rounded-2xl → rounded-lg (modals/large containers keep slight rounding)
    ("rounded-2xl", "rounded-lg"),
    
    # ============================================================
    # Ring/focus colors — amber instead of blue
    # ============================================================
    ("ring-blue-500", "ring-print-500"),
    ("ring-blue-400", "ring-print-400"),
    ("focus:ring-blue", "focus:ring-print"),
    
    # ============================================================
    # Status dot submitted/rejected (from approval workflow)
    # ============================================================
    # These should already be handled but ensure consistency
]

# More targeted replacements that need context
TARGETED = [
    # animate-pulse on status → use our custom statusPulse
    # (leave as-is, CSS handles it)
]

def process_file(filepath):
    global total_files, total_changes
    
    with open(filepath, "r") as f:
        content = f.read()
    
    original = content
    file_changes = 0
    
    for old, new in REPLACEMENTS:
        count = content.count(old)
        if count > 0:
            content = content.replace(old, new)
            file_changes += count
    
    if content != original:
        with open(filepath, "w") as f:
            f.write(content)
        total_files += 1
        total_changes += file_changes
        print(f"  ✓ {os.path.relpath(filepath, FRONTEND_SRC)}: {file_changes} changes")
    
    return file_changes > 0

# Walk all .jsx, .js files in src/
print("O.D.I.N. Forge Theme — Style Migration")
print("=" * 50)
print()

for root, dirs, files in os.walk(FRONTEND_SRC):
    # Skip node_modules if somehow present
    dirs[:] = [d for d in dirs if d != "node_modules"]
    
    for filename in sorted(files):
        if filename.endswith((".jsx", ".js")) and not filename.endswith(".config.js"):
            filepath = os.path.join(root, filename)
            process_file(filepath)

print()
print(f"Done: {total_changes} replacements across {total_files} files")
