#!/usr/bin/env python3
"""
QR Scanner Feature Deploy Script

Run on server:
    cd /opt/printfarm-scheduler/backend
    source venv/bin/activate
    python3 deploy_qr_scanner.py

What this does:
1. Adds scan-assign endpoint to main.py
2. Adds lookup endpoint to main.py  
3. Updates api.js with new methods
4. Adds jsQR import to QRScannerModal.jsx
5. Adds scan button to Printers page (slot editor)
"""

import os
import sys
import subprocess

BACKEND_PATH = "/opt/printfarm-scheduler/backend"
FRONTEND_PATH = "/opt/printfarm-scheduler/frontend"
MAIN_PY = f"{BACKEND_PATH}/main.py"
API_JS = f"{FRONTEND_PATH}/src/api.js"
PRINTERS_JSX = f"{FRONTEND_PATH}/src/pages/Printers.jsx"
SCANNER_JSX = f"{FRONTEND_PATH}/src/components/QRScannerModal.jsx"


def add_backend_endpoints():
    """Add scan-assign and lookup endpoints to main.py."""
    print("Adding backend endpoints...")
    
    with open(MAIN_PY, "r") as f:
        content = f.read()
    
    # Check if already added
    if "scan-assign" in content:
        print("  Backend endpoints already exist — skipping")
        return True
    
    # Find a good insertion point (after other spool endpoints)
    # Look for the spools section
    endpoint_code = '''

# ============== QR Scan-to-Assign ==============

class ScanAssignRequest(BaseModel):
    qr_code: str
    printer_id: int
    slot: int  # 0-indexed slot/gate number


class ScanAssignResponse(BaseModel):
    success: bool
    message: str
    spool_id: Optional[int] = None
    spool_name: Optional[str] = None
    printer_name: Optional[str] = None
    slot: Optional[int] = None


@app.post("/api/spools/scan-assign", response_model=ScanAssignResponse, tags=["Spools"])
def scan_assign_spool(
    data: ScanAssignRequest,
    db: Session = Depends(get_db)
):
    """
    Assign a spool to a printer slot by scanning its QR code.
    
    Used for:
    - Non-RFID printers (Kobra S1 with ACE)
    - Third-party filaments in Bambu AMS
    """
    # Find spool by QR code
    spool = db.query(Spool).filter(Spool.qr_code == data.qr_code).first()
    if not spool:
        return ScanAssignResponse(
            success=False,
            message=f"Spool not found: {data.qr_code}"
        )
    
    # Find printer
    printer = db.query(Printer).filter(Printer.id == data.printer_id).first()
    if not printer:
        return ScanAssignResponse(
            success=False,
            message=f"Printer not found: {data.printer_id}"
        )
    
    # Validate slot number
    if data.slot < 0 or data.slot >= (printer.slot_count or 4):
        return ScanAssignResponse(
            success=False,
            message=f"Invalid slot {data.slot} for {printer.name} (has {printer.slot_count or 4} slots)"
        )
    
    # Check if slot already has a spool assigned
    existing_slot = db.query(FilamentSlot).filter(
        FilamentSlot.printer_id == data.printer_id,
        FilamentSlot.slot_index == data.slot
    ).first()
    
    if existing_slot:
        # Update existing slot
        existing_slot.spool_id = spool.id
        existing_slot.filament_type = spool.filament.material if spool.filament else None
        existing_slot.color_name = spool.filament.name if spool.filament else None
        existing_slot.color_hex = spool.filament.color_hex if spool.filament else None
    else:
        # Create new slot entry
        new_slot = FilamentSlot(
            printer_id=data.printer_id,
            slot_index=data.slot,
            spool_id=spool.id,
            filament_type=spool.filament.material if spool.filament else None,
            color_name=spool.filament.name if spool.filament else None,
            color_hex=spool.filament.color_hex if spool.filament else None,
        )
        db.add(new_slot)
    
    # Update spool location
    spool.location_printer_id = data.printer_id
    spool.location_slot = data.slot
    
    # Clear any previous slot assignment for this spool on OTHER printers
    db.query(FilamentSlot).filter(
        FilamentSlot.spool_id == spool.id,
        FilamentSlot.printer_id != data.printer_id
    ).update({FilamentSlot.spool_id: None})
    
    db.commit()
    
    spool_name = f"{spool.filament.brand} {spool.filament.name}" if spool.filament else spool.qr_code
    
    return ScanAssignResponse(
        success=True,
        message=f"Assigned {spool_name} to {printer.name} slot {data.slot + 1}",
        spool_id=spool.id,
        spool_name=spool_name,
        printer_name=printer.name,
        slot=data.slot
    )


@app.get("/api/spools/lookup/{qr_code}", tags=["Spools"])
def lookup_spool_by_qr(qr_code: str, db: Session = Depends(get_db)):
    """Look up spool details by QR code."""
    spool = db.query(Spool).filter(Spool.qr_code == qr_code).first()
    if not spool:
        raise HTTPException(status_code=404, detail="Spool not found")
    
    return {
        "id": spool.id,
        "qr_code": spool.qr_code,
        "brand": spool.filament.brand if spool.filament else None,
        "name": spool.filament.name if spool.filament else None,
        "material": spool.filament.material if spool.filament else None,
        "color_hex": spool.filament.color_hex if spool.filament else None,
        "remaining_weight": spool.remaining_weight,
        "initial_weight": spool.initial_weight_g,
        "location_printer_id": spool.location_printer_id,
        "location_slot": spool.location_slot,
    }

'''
    
    # Find insertion point - look for "# ==" pattern to find section markers
    # Insert before the last section or at the end of routes
    insert_marker = "# ============== Startup"
    if insert_marker in content:
        content = content.replace(insert_marker, endpoint_code + "\n" + insert_marker)
    else:
        # Fallback: append before if __name__
        if 'if __name__' in content:
            content = content.replace('if __name__', endpoint_code + '\nif __name__')
        else:
            content += endpoint_code
    
    with open(MAIN_PY, "w") as f:
        f.write(content)
    
    print("  ✓ Added scan-assign and lookup endpoints")
    return True


def update_api_js():
    """Add lookup and scanAssign methods to api.js."""
    print("Updating api.js...")
    
    with open(API_JS, "r") as f:
        content = f.read()
    
    # Check if already added
    if "scanAssign" in content:
        print("  API methods already exist — skipping")
        return True
    
    # Find the spools export section and add methods
    # Look for "export const spools" or similar pattern
    old_pattern = "export const spools = {"
    if old_pattern not in content:
        # Try alternate patterns
        if "spools:" in content or "spools =" in content:
            print("  WARNING: spools section has different format, manual edit needed")
            print("  Add these methods to the spools object in api.js:")
            print("    lookup: (qrCode) => fetchAPI(`/api/spools/lookup/${qrCode}`),")
            print("    scanAssign: (qrCode, printerId, slot) => fetchAPI('/api/spools/scan-assign', {")
            print("      method: 'POST',")
            print("      body: JSON.stringify({ qr_code: qrCode, printer_id: printerId, slot: slot }),")
            print("    }),")
            return True
        else:
            print("  ERROR: Could not find spools section in api.js")
            return False
    
    # Add methods to spools object
    new_methods = '''export const spools = {
  lookup: (qrCode) => fetchAPI(`/api/spools/lookup/${qrCode}`),
  scanAssign: (qrCode, printerId, slot) => fetchAPI('/api/spools/scan-assign', {
    method: 'POST',
    body: JSON.stringify({ qr_code: qrCode, printer_id: printerId, slot: slot }),
  }),'''
    
    content = content.replace(old_pattern, new_methods)
    
    with open(API_JS, "w") as f:
        f.write(content)
    
    print("  ✓ Added lookup and scanAssign methods")
    return True


def fix_scanner_jsx():
    """Add jsQR import to the scanner component."""
    print("Fixing QRScannerModal.jsx...")
    
    if not os.path.exists(SCANNER_JSX):
        print("  ERROR: QRScannerModal.jsx not found — copy it first")
        return False
    
    with open(SCANNER_JSX, "r") as f:
        content = f.read()
    
    # Check if jsQR import exists
    if "import jsQR" in content:
        print("  jsQR import already exists — skipping")
        return True
    
    # Add import at top
    old_import = "import { useState"
    new_import = "import jsQR from 'jsqr';\nimport { useState"
    
    content = content.replace(old_import, new_import, 1)
    
    # Also update the scanFrame function to not check typeof
    old_check = "if (typeof jsQR !== 'undefined') {"
    new_check = "{"
    content = content.replace(old_check, new_check)
    
    # Remove the closing brace of that if
    # This is tricky - let's just make sure the jsQR call works
    
    with open(SCANNER_JSX, "w") as f:
        f.write(content)
    
    print("  ✓ Added jsQR import")
    return True


def install_jsqr():
    """Install jsQR npm package."""
    print("Installing jsQR package...")
    
    os.chdir(FRONTEND_PATH)
    result = subprocess.run(
        ["npm", "install", "jsqr"],
        capture_output=True,
        text=True
    )
    
    if result.returncode == 0:
        print("  ✓ jsQR installed")
        return True
    else:
        print(f"  ERROR: npm install failed: {result.stderr}")
        return False


def add_scan_button_to_printers():
    """Add scan button to Printers page."""
    print("Adding scan button to Printers page...")
    
    with open(PRINTERS_JSX, "r") as f:
        content = f.read()
    
    # Check if already added
    if "QRScannerModal" in content:
        print("  Scanner already integrated — skipping")
        return True
    
    # Add import
    old_import = "import { useState"
    new_import = "import QRScannerModal from '../components/QRScannerModal';\nimport { useState"
    
    if old_import in content:
        content = content.replace(old_import, new_import, 1)
    
    # Add QrCode to lucide imports
    if "lucide-react" in content:
        # Find the lucide import line and add QrCode if not present
        if "QrCode" not in content:
            content = content.replace(
                "} from 'lucide-react'",
                ", QrCode } from 'lucide-react'"
            )
    
    # We need to add state and the modal - this is complex
    # For now, let's just add the import and print instructions
    
    with open(PRINTERS_JSX, "w") as f:
        f.write(content)
    
    print("  ✓ Added imports")
    print("")
    print("  MANUAL STEP NEEDED:")
    print("  Add this state to the Printers component:")
    print("    const [showScanner, setShowScanner] = useState(false);")
    print("    const [scannerPrinter, setScannerPrinter] = useState(null);")
    print("")
    print("  Add this button where you want the scan option (e.g., in printer card):")
    print('    <button onClick={() => { setScannerPrinter(printer.id); setShowScanner(true); }}')
    print('      className="p-1.5 text-farm-400 hover:bg-farm-800 rounded">')
    print('      <QrCode className="w-4 h-4" />')
    print('    </button>')
    print("")
    print("  Add this modal at the end of the component (before closing </div>):")
    print("    <QRScannerModal")
    print("      isOpen={showScanner}")
    print("      onClose={() => setShowScanner(false)}")
    print("      preselectedPrinter={scannerPrinter}")
    print("      onAssigned={() => loadPrinters()}")
    print("    />")
    
    return True


def main():
    print("=" * 50)
    print("QR Scanner Feature Deploy")
    print("=" * 50)
    print()
    
    # Check if scanner component exists
    if not os.path.exists(SCANNER_JSX):
        print(f"ERROR: {SCANNER_JSX} not found")
        print("Copy QRScannerModal.jsx to that location first:")
        print(f"  scp QRScannerModal.jsx root@server:{SCANNER_JSX}")
        sys.exit(1)
    
    steps = [
        ("Backend endpoints", add_backend_endpoints),
        ("Install jsQR", install_jsqr),
        ("Fix scanner JSX", fix_scanner_jsx),
        ("Update api.js", update_api_js),
        ("Printers page integration", add_scan_button_to_printers),
    ]
    
    for name, func in steps:
        print(f"\n--- {name} ---")
        if not func():
            print(f"FAILED at step: {name}")
            sys.exit(1)
    
    print("\n" + "=" * 50)
    print("Deploy complete!")
    print()
    print("Restart services:")
    print("  systemctl restart printfarm-backend")
    print("  cd /opt/printfarm-scheduler/frontend && npm run build")
    print()
    print("Or for dev mode:")
    print("  systemctl restart printfarm-backend")
    print("  systemctl restart printfarm-frontend")
    print("=" * 50)


if __name__ == "__main__":
    main()
