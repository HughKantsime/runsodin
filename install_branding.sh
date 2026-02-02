#!/bin/bash
# ============================================================
# PrintFarm Scheduler - White-Label Branding Install Script
# v0.9.1 - Run on 192.168.70.200 as root
# ============================================================
set -e

PROJECT="/opt/printfarm-scheduler"
BACKEND="$PROJECT/backend"
FRONTEND="$PROJECT/frontend"

echo "========================================="
echo "  Installing White-Label Branding v0.9.1"
echo "========================================="

# ---- Verify new files are in place ----
echo ""
echo "[1/7] Checking new files..."
for f in "$BACKEND/branding.py" "$FRONTEND/src/BrandingContext.jsx" "$FRONTEND/src/pages/Branding.jsx"; do
  if [ ! -f "$f" ]; then
    echo "ERROR: Missing $f"
    echo "SCP the files first before running this script."
    exit 1
  fi
done
echo "  ✓ All new files present"

# ---- Backend: main.py edits ----
echo ""
echo "[2/7] Patching backend/main.py..."

# Edit 1: Add imports after "import httpx"
if ! grep -q "from branding import" "$BACKEND/main.py"; then
  sed -i '/^import httpx$/a\import shutil\nfrom fastapi.staticfiles import StaticFiles\nfrom branding import Branding, get_or_create_branding, branding_to_dict, UPDATABLE_FIELDS' "$BACKEND/main.py"
  echo "  ✓ Added branding imports"
else
  echo "  ⏭ Branding imports already present"
fi

# Edit 2: Mount static files after CORS middleware block
if ! grep -q 'app.mount("/static"' "$BACKEND/main.py"; then
  sed -i '/^).*# end CORS\|^app\.add_middleware(/,/^)$/{
    /^)$/a\
\
# Static files for branding assets (logos, favicons)\
static_dir = os.path.join(os.path.dirname(__file__), "static")\
os.makedirs(static_dir, exist_ok=True)\
app.mount("/static", StaticFiles(directory=static_dir), name="static")
  }' "$BACKEND/main.py"
  # If the sed above didn't match (CORS block varies), try inserting after allow_headers line
  if ! grep -q 'app.mount("/static"' "$BACKEND/main.py"; then
    sed -i '/allow_headers=\["\*"\],/,/^)$/{
      /^)$/a\
\
# Static files for branding assets (logos, favicons)\
static_dir = os.path.join(os.path.dirname(__file__), "static")\
os.makedirs(static_dir, exist_ok=True)\
app.mount("/static", StaticFiles(directory=static_dir), name="static")
    }' "$BACKEND/main.py"
  fi
  echo "  ✓ Mounted static files"
else
  echo "  ⏭ Static mount already present"
fi

# Edit 3: Append branding routes to end of file
if ! grep -q 'async def get_branding' "$BACKEND/main.py"; then
  cat >> "$BACKEND/main.py" << 'BRANDING_ROUTES'


# ============== Branding (White-Label) ==============

@app.get("/api/branding", tags=["Branding"])
async def get_branding(db: Session = Depends(get_db)):
    """Get branding config. PUBLIC - no auth required."""
    return branding_to_dict(get_or_create_branding(db))


@app.put("/api/branding", tags=["Branding"])
async def update_branding(data: dict, db: Session = Depends(get_db)):
    """Update branding config. Admin only."""
    branding = get_or_create_branding(db)
    for key, value in data.items():
        if key in UPDATABLE_FIELDS:
            setattr(branding, key, value)
    branding.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(branding)
    return branding_to_dict(branding)


@app.post("/api/branding/logo", tags=["Branding"])
async def upload_logo(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Upload brand logo. Admin only."""
    allowed = {"image/png", "image/jpeg", "image/svg+xml", "image/webp"}
    if file.content_type not in allowed:
        raise HTTPException(status_code=400, detail="File type not allowed")
    upload_dir = os.path.join(os.path.dirname(__file__), "static", "branding")
    os.makedirs(upload_dir, exist_ok=True)
    ext = file.filename.split(".")[-1] if "." in file.filename else "png"
    filename = f"logo.{ext}"
    with open(os.path.join(upload_dir, filename), "wb") as f:
        shutil.copyfileobj(file.file, f)
    branding = get_or_create_branding(db)
    branding.logo_url = f"/static/branding/{filename}"
    db.commit()
    return {"logo_url": branding.logo_url}


@app.post("/api/branding/favicon", tags=["Branding"])
async def upload_favicon(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Upload favicon. Admin only."""
    allowed = {"image/png", "image/x-icon", "image/svg+xml", "image/webp"}
    if file.content_type not in allowed:
        raise HTTPException(status_code=400, detail="File type not allowed")
    upload_dir = os.path.join(os.path.dirname(__file__), "static", "branding")
    os.makedirs(upload_dir, exist_ok=True)
    ext = file.filename.split(".")[-1] if "." in file.filename else "png"
    filename = f"favicon.{ext}"
    with open(os.path.join(upload_dir, filename), "wb") as f:
        shutil.copyfileobj(file.file, f)
    branding = get_or_create_branding(db)
    branding.favicon_url = f"/static/branding/{filename}"
    db.commit()
    return {"favicon_url": branding.favicon_url}


@app.delete("/api/branding/logo", tags=["Branding"])
async def remove_logo(db: Session = Depends(get_db)):
    """Remove brand logo. Admin only."""
    branding = get_or_create_branding(db)
    if branding.logo_url:
        filepath = os.path.join(os.path.dirname(__file__), branding.logo_url.lstrip("/"))
        if os.path.exists(filepath):
            os.remove(filepath)
    branding.logo_url = None
    db.commit()
    return {"logo_url": None}
BRANDING_ROUTES
  echo "  ✓ Added branding API routes"
else
  echo "  ⏭ Branding routes already present"
fi

# ---- Create static directory ----
echo ""
echo "[3/7] Creating static directories..."
mkdir -p "$BACKEND/static/branding"
echo "  ✓ Created backend/static/branding/"

# ---- Frontend: index.css - Add CSS variable defaults ----
echo ""
echo "[4/7] Patching frontend/src/index.css..."
if ! grep -q '\-\-brand-primary' "$FRONTEND/src/index.css"; then
  # Prepend CSS variables to the top of the file
  TMPFILE=$(mktemp)
  cat > "$TMPFILE" << 'CSS_VARS'
:root {
  /* Brand colors - overridden at runtime by BrandingContext */
  --brand-primary: #22c55e;
  --brand-accent: #4ade80;
  --brand-sidebar-bg: #1a1917;
  --brand-sidebar-border: #3b3934;
  --brand-sidebar-text: #8a8679;
  --brand-sidebar-active-bg: #3b3934;
  --brand-sidebar-active-text: #4ade80;
  --brand-content-bg: #1a1917;
  --brand-card-bg: #33312d;
  --brand-card-border: #3b3934;
  --brand-text-primary: #e5e4e1;
  --brand-text-secondary: #8a8679;
  --brand-text-muted: #58554a;
  --brand-input-bg: #3b3934;
  --brand-input-border: #47453d;
  /* Brand fonts */
  --brand-font-display: 'Space Grotesk', system-ui, sans-serif;
  --brand-font-body: 'Inter', system-ui, sans-serif;
  --brand-font-mono: 'JetBrains Mono', 'Fira Code', monospace;
}

CSS_VARS
  cat "$FRONTEND/src/index.css" >> "$TMPFILE"
  mv "$TMPFILE" "$FRONTEND/src/index.css"
  echo "  ✓ Added CSS custom property defaults"
else
  echo "  ⏭ CSS variables already present"
fi

# ---- Frontend: main.jsx - Wrap with BrandingProvider ----
echo ""
echo "[5/7] Patching frontend/src/main.jsx..."
if ! grep -q 'BrandingProvider' "$FRONTEND/src/main.jsx"; then
  # Add import
  sed -i "/import App from/a\\import { BrandingProvider } from './BrandingContext'" "$FRONTEND/src/main.jsx"
  # Wrap App with BrandingProvider
  sed -i 's|<App />|<BrandingProvider><App /></BrandingProvider>|' "$FRONTEND/src/main.jsx"
  echo "  ✓ Wrapped App with BrandingProvider"
else
  echo "  ⏭ BrandingProvider already present"
fi

# ---- Frontend: App.jsx - Add branding to sidebar + route ----
echo ""
echo "[6/7] Patching frontend/src/App.jsx..."

# Add imports if not present
if ! grep -q 'useBranding' "$FRONTEND/src/App.jsx"; then
  sed -i "/^import.*lucide-react/s/}/,Palette }/" "$FRONTEND/src/App.jsx"
  # Add after the lucide import line
  sed -i "/from 'lucide-react'/a\\import { useBranding } from './BrandingContext'\nimport Branding from './pages/Branding'" "$FRONTEND/src/App.jsx"
  echo "  ✓ Added branding imports to App.jsx"
else
  echo "  ⏭ Branding imports already in App.jsx"
fi

# Add route if not present
if ! grep -q 'path="/branding"' "$FRONTEND/src/App.jsx"; then
  # Add route before the catch-all or last Route
  sed -i '/<Route.*path="\/settings"/a\            <Route path="/branding" element={<Branding />} />' "$FRONTEND/src/App.jsx"
  echo "  ✓ Added /branding route"
else
  echo "  ⏭ Branding route already present"
fi

# ---- Frontend: Login.jsx - Use branding for title ----
echo ""
echo "[7/7] Patching frontend/src/pages/Login.jsx..."
if ! grep -q 'useBranding' "$FRONTEND/src/pages/Login.jsx"; then
  # Add import after the first import line
  sed -i "1a\\import { useBranding } from '../BrandingContext'" "$FRONTEND/src/pages/Login.jsx"
  # Add hook usage after the first useState
  sed -i '/const \[username, setUsername\]/i\  const branding = useBranding()' "$FRONTEND/src/pages/Login.jsx"
  # Replace hardcoded PRINTFARM title
  sed -i 's|>PRINTFARM</h1>|>{branding.app_name.toUpperCase()}</h1>|' "$FRONTEND/src/pages/Login.jsx"
  # Replace hardcoded Scheduler subtitle
  sed -i 's|>Scheduler</p>|>{branding.app_subtitle}</p>|' "$FRONTEND/src/pages/Login.jsx"
  echo "  ✓ Patched Login.jsx with branding"
else
  echo "  ⏭ Login.jsx already patched"
fi

echo ""
echo "========================================="
echo "  All patches applied!"
echo "========================================="
echo ""
echo "Next steps:"
echo "  1. cd $FRONTEND && npm run build"
echo "  2. Restart the backend service"
echo "  3. Navigate to Admin → Branding"
echo ""
