#!/usr/bin/env python3
"""
O.D.I.N. — i18n (Internationalization) System
Lightweight React-based translation system:
- i18n context provider with useTranslation hook
- JSON translation files per language
- Language selector in Settings
- Ships with EN (complete), DE, JA, ES (core strings)
- System config stores selected language
- Fallback: missing keys return English string
"""

import os
import json
import sqlite3

BASE = "/opt/printfarm-scheduler"
FRONTEND = f"{BASE}/frontend/src"
DB_PATH = f"{BASE}/backend/printfarm.db"
MAIN_PATH = f"{BASE}/backend/main.py"
API_PATH = f"{FRONTEND}/api.js"

print("=" * 60)
print("  O.D.I.N. — i18n Internationalization")
print("=" * 60)
print()

# ============================================================
# 1. Create translation files
# ============================================================

i18n_dir = f"{FRONTEND}/i18n"
os.makedirs(i18n_dir, exist_ok=True)

# English (complete — this is the source of truth)
en = {
    # Navigation
    "nav.dashboard": "Dashboard",
    "nav.printers": "Printers",
    "nav.cameras": "Cameras",
    "nav.timeline": "Timeline",
    "nav.orders": "Orders",
    "nav.jobs": "Jobs",
    "nav.upload": "Upload",
    "nav.maintenance": "Maintenance",
    "nav.products": "Products",
    "nav.models": "Models",
    "nav.spools": "Spools",
    "nav.analytics": "Analytics",
    "nav.calculator": "Calculator",
    "nav.settings": "Settings",
    "nav.admin": "Admin",
    "nav.permissions": "Permissions",
    "nav.branding": "Branding",
    "nav.alerts": "Alerts",
    "nav.work": "WORK",
    "nav.library": "LIBRARY",
    "nav.analyze": "ANALYZE",
    "nav.system": "SYSTEM",

    # Common actions
    "common.save": "Save",
    "common.cancel": "Cancel",
    "common.delete": "Delete",
    "common.edit": "Edit",
    "common.create": "Create",
    "common.close": "Close",
    "common.search": "Search",
    "common.filter": "Filter",
    "common.export": "Export",
    "common.import": "Import",
    "common.loading": "Loading...",
    "common.confirm": "Confirm",
    "common.yes": "Yes",
    "common.no": "No",
    "common.back": "Back",
    "common.next": "Next",
    "common.done": "Done",
    "common.add": "Add",
    "common.remove": "Remove",
    "common.refresh": "Refresh",
    "common.actions": "Actions",
    "common.status": "Status",
    "common.name": "Name",
    "common.type": "Type",
    "common.notes": "Notes",
    "common.none": "None",

    # Dashboard
    "dashboard.title": "Dashboard",
    "dashboard.fleet_status": "Fleet Status",
    "dashboard.active_prints": "Active Prints",
    "dashboard.queue": "Queue",
    "dashboard.completed_today": "Completed Today",
    "dashboard.printers_online": "printers online",
    "dashboard.no_active_prints": "No active prints",

    # Printers
    "printers.title": "Printers",
    "printers.add": "Add Printer",
    "printers.online": "Online",
    "printers.offline": "Offline",
    "printers.idle": "Idle",
    "printers.printing": "Printing",
    "printers.paused": "Paused",
    "printers.bed_temp": "Bed",
    "printers.nozzle_temp": "Nozzle",
    "printers.progress": "Progress",
    "printers.remaining": "Remaining",
    "printers.smart_plug": "Smart Plug",
    "printers.power_on": "Power On",
    "printers.power_off": "Power Off",

    # Jobs
    "jobs.title": "Jobs",
    "jobs.all": "All Jobs",
    "jobs.order_jobs": "Order Jobs",
    "jobs.ad_hoc": "Ad-hoc",
    "jobs.awaiting_approval": "Awaiting Approval",
    "jobs.schedule": "Schedule",
    "jobs.print_again": "Print Again",
    "jobs.pending": "Pending",
    "jobs.scheduled": "Scheduled",
    "jobs.printing": "Printing",
    "jobs.completed": "Completed",
    "jobs.failed": "Failed",
    "jobs.cancelled": "Cancelled",
    "jobs.submitted": "Submitted",
    "jobs.approved": "Approved",
    "jobs.rejected": "Rejected",

    # Models
    "models.title": "Models",
    "models.upload": "Upload .3mf",
    "models.favorites": "Favorites",
    "models.all": "All Models",
    "models.view_3d": "View 3D Model",
    "models.cost": "Cost",
    "models.price": "Price",
    "models.build_time": "Build Time",

    # Orders
    "orders.title": "Orders",
    "orders.new": "New Order",
    "orders.pending": "Pending",
    "orders.in_progress": "In Progress",
    "orders.fulfilled": "Fulfilled",
    "orders.shipped": "Shipped",
    "orders.revenue": "Revenue",
    "orders.profit": "Profit",
    "orders.margin": "Margin",

    # Spools
    "spools.title": "Spools",
    "spools.add": "Add Spool",
    "spools.remaining": "Remaining",
    "spools.material": "Material",
    "spools.brand": "Brand",
    "spools.color": "Color",
    "spools.low_warning": "Low spool warning",

    # Cameras
    "cameras.title": "Cameras",
    "cameras.control_room": "Control Room",
    "cameras.no_cameras": "No cameras configured",

    # Settings
    "settings.title": "Settings",
    "settings.general": "General",
    "settings.alerts": "Alerts",
    "settings.smtp": "SMTP",
    "settings.sso": "SSO",
    "settings.webhooks": "Webhooks",
    "settings.advanced": "Advanced",
    "settings.about": "About",
    "settings.language": "Language",
    "settings.language_desc": "Select the display language for the interface",
    "settings.energy_rate": "Energy Cost (per kWh)",
    "settings.energy_rate_desc": "Used for electricity cost calculation with smart plugs",

    # Alerts
    "alerts.title": "Alerts",
    "alerts.mark_read": "Mark as Read",
    "alerts.mark_all_read": "Mark All Read",
    "alerts.no_alerts": "No alerts",
    "alerts.print_complete": "Print Complete",
    "alerts.print_failed": "Print Failed",
    "alerts.spool_low": "Spool Low",
    "alerts.maintenance_due": "Maintenance Due",

    # Analytics
    "analytics.title": "Analytics",
    "analytics.revenue": "Revenue",
    "analytics.costs": "Costs",
    "analytics.profit": "Profit",
    "analytics.jobs_completed": "Jobs Completed",
    "analytics.print_hours": "Print Hours",
    "analytics.filament_used": "Filament Used",

    # Upload
    "upload.title": "Upload",
    "upload.drag_drop": "Drag and drop .3mf files here",
    "upload.or_click": "or click to browse",
    "upload.schedule_now": "Schedule Now",
    "upload.submit_approval": "Submit for Approval",
    "upload.objects_detected": "Objects Detected",

    # Maintenance
    "maintenance.title": "Maintenance",
    "maintenance.schedule": "Schedule Maintenance",
    "maintenance.history": "History",
    "maintenance.due": "Due",
    "maintenance.overdue": "Overdue",

    # Emergency
    "emergency.stop": "Emergency Stop",
    "emergency.pause": "Pause",
    "emergency.resume": "Resume",

    # Login
    "login.title": "Sign In",
    "login.username": "Username",
    "login.password": "Password",
    "login.sign_in": "Sign In",
    "login.sso": "Sign in with Microsoft",

    # Setup wizard
    "setup.welcome": "Welcome to O.D.I.N.",
    "setup.create_admin": "Create Admin Account",
    "setup.name_instance": "Name Your Instance",
    "setup.add_printer": "Add Your First Printer",
    "setup.done": "You're All Set!",

    # License
    "license.community": "Community",
    "license.pro": "Pro",
    "license.education": "Education",
    "license.enterprise": "Enterprise",
    "license.upgrade": "Upgrade to Pro",
    "license.features_locked": "This feature requires a Pro license",

    # AMS
    "ams.humidity": "Humidity",
    "ams.temperature": "Temperature",
    "ams.dry": "Dry",
    "ams.low": "Low",
    "ams.moderate": "Moderate",
    "ams.high": "High",
    "ams.wet": "Wet",

    # Time
    "time.hours": "hours",
    "time.minutes": "minutes",
    "time.seconds": "seconds",
    "time.ago": "ago",
    "time.remaining": "remaining",
}

# German
de = {
    "nav.dashboard": "Übersicht",
    "nav.printers": "Drucker",
    "nav.cameras": "Kameras",
    "nav.timeline": "Zeitplan",
    "nav.orders": "Aufträge",
    "nav.jobs": "Druckjobs",
    "nav.upload": "Hochladen",
    "nav.maintenance": "Wartung",
    "nav.products": "Produkte",
    "nav.models": "Modelle",
    "nav.spools": "Spulen",
    "nav.analytics": "Analyse",
    "nav.calculator": "Rechner",
    "nav.settings": "Einstellungen",
    "nav.admin": "Admin",
    "nav.permissions": "Berechtigungen",
    "nav.branding": "Branding",
    "nav.alerts": "Benachrichtigungen",
    "nav.work": "ARBEIT",
    "nav.library": "BIBLIOTHEK",
    "nav.analyze": "ANALYSE",
    "nav.system": "SYSTEM",
    "common.save": "Speichern",
    "common.cancel": "Abbrechen",
    "common.delete": "Löschen",
    "common.edit": "Bearbeiten",
    "common.create": "Erstellen",
    "common.close": "Schließen",
    "common.search": "Suchen",
    "common.loading": "Laden...",
    "common.confirm": "Bestätigen",
    "common.yes": "Ja",
    "common.no": "Nein",
    "common.back": "Zurück",
    "common.next": "Weiter",
    "common.done": "Fertig",
    "dashboard.title": "Übersicht",
    "dashboard.fleet_status": "Flottenübersicht",
    "dashboard.active_prints": "Aktive Drucke",
    "dashboard.completed_today": "Heute fertig",
    "dashboard.printers_online": "Drucker online",
    "printers.title": "Drucker",
    "printers.add": "Drucker hinzufügen",
    "printers.online": "Online",
    "printers.offline": "Offline",
    "printers.printing": "Druckt",
    "printers.bed_temp": "Bett",
    "printers.nozzle_temp": "Düse",
    "jobs.title": "Druckjobs",
    "jobs.all": "Alle Jobs",
    "jobs.completed": "Abgeschlossen",
    "jobs.failed": "Fehlgeschlagen",
    "jobs.schedule": "Einplanen",
    "models.title": "Modelle",
    "models.upload": ".3mf hochladen",
    "orders.title": "Aufträge",
    "orders.revenue": "Umsatz",
    "orders.profit": "Gewinn",
    "spools.title": "Spulen",
    "spools.remaining": "Verbleibend",
    "cameras.title": "Kameras",
    "cameras.control_room": "Kontrollraum",
    "settings.title": "Einstellungen",
    "settings.language": "Sprache",
    "alerts.title": "Benachrichtigungen",
    "emergency.stop": "Not-Halt",
    "login.sign_in": "Anmelden",
    "upload.title": "Hochladen",
    "maintenance.title": "Wartung",
    "analytics.title": "Analyse",
}

# Japanese
ja = {
    "nav.dashboard": "ダッシュボード",
    "nav.printers": "プリンター",
    "nav.cameras": "カメラ",
    "nav.timeline": "タイムライン",
    "nav.orders": "注文",
    "nav.jobs": "ジョブ",
    "nav.upload": "アップロード",
    "nav.maintenance": "メンテナンス",
    "nav.products": "製品",
    "nav.models": "モデル",
    "nav.spools": "スプール",
    "nav.analytics": "分析",
    "nav.settings": "設定",
    "nav.work": "作業",
    "nav.library": "ライブラリ",
    "nav.analyze": "分析",
    "nav.system": "システム",
    "common.save": "保存",
    "common.cancel": "キャンセル",
    "common.delete": "削除",
    "common.edit": "編集",
    "common.search": "検索",
    "common.loading": "読み込み中...",
    "common.yes": "はい",
    "common.no": "いいえ",
    "dashboard.title": "ダッシュボード",
    "dashboard.active_prints": "印刷中",
    "printers.title": "プリンター",
    "printers.online": "オンライン",
    "printers.offline": "オフライン",
    "printers.printing": "印刷中",
    "jobs.title": "ジョブ",
    "jobs.completed": "完了",
    "jobs.failed": "失敗",
    "models.title": "モデル",
    "orders.title": "注文",
    "spools.title": "スプール",
    "cameras.title": "カメラ",
    "settings.title": "設定",
    "settings.language": "言語",
    "alerts.title": "アラート",
    "emergency.stop": "緊急停止",
    "login.sign_in": "ログイン",
    "analytics.title": "分析",
}

# Spanish
es = {
    "nav.dashboard": "Panel",
    "nav.printers": "Impresoras",
    "nav.cameras": "Cámaras",
    "nav.timeline": "Cronograma",
    "nav.orders": "Pedidos",
    "nav.jobs": "Trabajos",
    "nav.upload": "Subir",
    "nav.maintenance": "Mantenimiento",
    "nav.products": "Productos",
    "nav.models": "Modelos",
    "nav.spools": "Bobinas",
    "nav.analytics": "Análisis",
    "nav.settings": "Configuración",
    "nav.work": "TRABAJO",
    "nav.library": "BIBLIOTECA",
    "nav.analyze": "ANÁLISIS",
    "nav.system": "SISTEMA",
    "common.save": "Guardar",
    "common.cancel": "Cancelar",
    "common.delete": "Eliminar",
    "common.edit": "Editar",
    "common.search": "Buscar",
    "common.loading": "Cargando...",
    "common.yes": "Sí",
    "common.no": "No",
    "dashboard.title": "Panel",
    "dashboard.active_prints": "Impresiones Activas",
    "printers.title": "Impresoras",
    "printers.online": "En línea",
    "printers.offline": "Fuera de línea",
    "printers.printing": "Imprimiendo",
    "jobs.title": "Trabajos",
    "jobs.completed": "Completado",
    "jobs.failed": "Fallido",
    "models.title": "Modelos",
    "orders.title": "Pedidos",
    "orders.revenue": "Ingresos",
    "orders.profit": "Ganancia",
    "spools.title": "Bobinas",
    "cameras.title": "Cámaras",
    "settings.title": "Configuración",
    "settings.language": "Idioma",
    "alerts.title": "Alertas",
    "emergency.stop": "Parada de Emergencia",
    "login.sign_in": "Iniciar Sesión",
    "analytics.title": "Análisis",
}

langs = {"en": en, "de": de, "ja": ja, "es": es}
for code, translations in langs.items():
    path = f"{i18n_dir}/{code}.json"
    with open(path, "w") as f:
        json.dump(translations, f, ensure_ascii=False, indent=2)

print(f"[1/5] ✅ Created translation files: {', '.join(langs.keys())}")
print(f"       EN: {len(en)} keys (complete)")
print(f"       DE: {len(de)} keys, JA: {len(ja)} keys, ES: {len(es)} keys")

# ============================================================
# 2. Create i18n context + hook
# ============================================================

i18n_context = '''import { createContext, useContext, useState, useEffect, useCallback } from 'react'
import { fetchAPI } from '../api'

// Import translations
import en from '../i18n/en.json'
import de from '../i18n/de.json'
import ja from '../i18n/ja.json'
import es from '../i18n/es.json'

const translations = { en, de, ja, es }

const LANGUAGES = [
  { code: 'en', name: 'English', flag: '🇺🇸' },
  { code: 'de', name: 'Deutsch', flag: '🇩🇪' },
  { code: 'ja', name: '日本語', flag: '🇯🇵' },
  { code: 'es', name: 'Español', flag: '🇪🇸' },
]

const I18nContext = createContext()

export function I18nProvider({ children }) {
  const [locale, setLocale] = useState(() => {
    return localStorage.getItem('odin-locale') || 'en'
  })

  // Load from server config on mount
  useEffect(() => {
    fetchAPI('/settings/language').then(data => {
      if (data && data.language && translations[data.language]) {
        setLocale(data.language)
        localStorage.setItem('odin-locale', data.language)
      }
    }).catch(() => {})
  }, [])

  const changeLocale = useCallback(async (newLocale) => {
    if (!translations[newLocale]) return
    setLocale(newLocale)
    localStorage.setItem('odin-locale', newLocale)
    // Save to server
    try {
      await fetchAPI('/settings/language', {
        method: 'PUT',
        body: JSON.stringify({ language: newLocale })
      })
    } catch (e) {
      console.warn('Failed to save language preference:', e)
    }
  }, [])

  const t = useCallback((key, fallback) => {
    const current = translations[locale]
    if (current && current[key]) return current[key]
    // Fallback to English
    if (translations.en[key]) return translations.en[key]
    // Fallback to provided default or key itself
    return fallback || key
  }, [locale])

  return (
    <I18nContext.Provider value={{ locale, setLocale: changeLocale, t, LANGUAGES }}>
      {children}
    </I18nContext.Provider>
  )
}

export function useTranslation() {
  const ctx = useContext(I18nContext)
  if (!ctx) {
    // Fallback for components outside provider
    return {
      t: (key, fallback) => fallback || key,
      locale: 'en',
      setLocale: () => {},
      LANGUAGES: []
    }
  }
  return ctx
}

export { LANGUAGES }
export default I18nContext
'''

ctx_path = f"{FRONTEND}/contexts/I18nContext.jsx"
os.makedirs(f"{FRONTEND}/contexts", exist_ok=True)
with open(ctx_path, "w") as f:
    f.write(i18n_context)
print("[2/5] ✅ Created I18nContext.jsx with useTranslation hook")

# ============================================================
# 3. Add language API endpoints to backend
# ============================================================

with open(MAIN_PATH, "r") as f:
    main_content = f.read()

lang_endpoints = '''

# ============== Language / i18n ==============

@app.get("/api/settings/language", tags=["Settings"])
async def get_language(db: Session = Depends(get_db)):
    """Get current interface language."""
    result = db.execute(text("SELECT value FROM system_config WHERE key = 'language'")).fetchone()
    return {"language": result[0] if result else "en"}


@app.put("/api/settings/language", tags=["Settings"])
async def set_language(request: Request, db: Session = Depends(get_db)):
    """Set interface language."""
    data = await request.json()
    lang = data.get("language", "en")
    supported = ["en", "de", "ja", "es"]
    if lang not in supported:
        raise HTTPException(400, f"Unsupported language. Choose from: {', '.join(supported)}")
    db.execute(text(
        "INSERT OR REPLACE INTO system_config (key, value) VALUES ('language', :lang)"
    ), {"lang": lang})
    db.commit()
    return {"language": lang}

'''

api_changes = []
if "/api/settings/language" not in main_content:
    for marker in ["# ============== AMS Environmental", "# ============== Smart Plug", "# ============== 3D Model Viewer", "# ============== Maintenance"]:
        if marker in main_content:
            main_content = main_content.replace(marker, lang_endpoints + "\n" + marker)
            api_changes.append("Added language API endpoints")
            break
    else:
        main_content += lang_endpoints
        api_changes.append("Added language API endpoints (appended)")

    with open(MAIN_PATH, "w") as f:
        f.write(main_content)

print(f"[3/5] Patched main.py:")
for c in api_changes:
    print(f"  ✅ {c}")
if not api_changes:
    print("  ⚠️  Language endpoints already exist")

# ============================================================
# 4. Wrap App.jsx with I18nProvider
# ============================================================

APP_PATH = f"{FRONTEND}/App.jsx"
with open(APP_PATH, "r") as f:
    app_content = f.read()

app_changes = []

# Add import
if "I18nProvider" not in app_content:
    # Find last import line
    import_lines = [i for i, line in enumerate(app_content.split('\n')) if line.strip().startswith('import ')]
    if import_lines:
        lines = app_content.split('\n')
        last_import = import_lines[-1]
        lines.insert(last_import + 1, "import { I18nProvider } from './contexts/I18nContext'")
        app_content = '\n'.join(lines)
        app_changes.append("Added I18nProvider import")

    # Wrap the top-level return — find the outermost provider/fragment
    # Look for <LicenseProvider> or the first <div or <Router
    # Strategy: wrap the entire return content
    if "<I18nProvider>" not in app_content:
        # Find the return ( and wrap content
        # Simple approach: wrap around LicenseProvider if it exists
        if "<LicenseProvider>" in app_content:
            app_content = app_content.replace("<LicenseProvider>", "<I18nProvider>\n    <LicenseProvider>", 1)
            app_content = app_content.replace("</LicenseProvider>", "</LicenseProvider>\n    </I18nProvider>", 1)
            app_changes.append("Wrapped app with I18nProvider")
        else:
            # Wrap around Router or first div
            for wrapper in ["<BrowserRouter>", "<Router>"]:
                if wrapper in app_content:
                    closing = wrapper.replace("<", "</")
                    app_content = app_content.replace(wrapper, f"<I18nProvider>\n    {wrapper}", 1)
                    # Find last occurrence of closing tag
                    last_close = app_content.rfind(closing)
                    if last_close > 0:
                        app_content = app_content[:last_close + len(closing)] + "\n    </I18nProvider>" + app_content[last_close + len(closing):]
                    app_changes.append("Wrapped app with I18nProvider")
                    break

    with open(APP_PATH, "w") as f:
        f.write(app_content)

print(f"[4/5] Patched App.jsx:")
for c in app_changes:
    print(f"  ✅ {c}")
if not app_changes:
    print("  ⚠️  Already patched")

# ============================================================
# 5. Add API functions to api.js
# ============================================================

with open(API_PATH, "r") as f:
    api_js = f.read()

if "getLanguage" not in api_js:
    api_js += '''
// ---- Language / i18n ----
export const getLanguage = () => fetchAPI('/settings/language')
export const setLanguage = (lang) => fetchAPI('/settings/language', { method: 'PUT', body: JSON.stringify({ language: lang }) })
'''
    with open(API_PATH, "w") as f:
        f.write(api_js)
    print("[5/5] ✅ Added language API functions to api.js")
else:
    print("[5/5] ⚠️  Language API functions already in api.js")

print()
print("=" * 60)
print("  i18n System Complete")
print()
print("  Languages: English, Deutsch, 日本語, Español")
print("  Usage in components:")
print("    import { useTranslation } from '../contexts/I18nContext'")
print("    const { t } = useTranslation()")
print("    <h1>{t('dashboard.title')}</h1>")
print()
print("  Adding new languages:")
print("    1. Create src/i18n/xx.json")
print("    2. Import in I18nContext.jsx")
print("    3. Add to translations object + LANGUAGES array")
print()
print("  Next: npm run build && systemctl restart printfarm-backend")
print("=" * 60)
