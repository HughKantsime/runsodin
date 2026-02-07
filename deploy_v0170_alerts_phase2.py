"""
v0.17.0 Phase 2 Deploy Script — Frontend Alerts UI
Patches: App.jsx, api.js
Creates: AlertBell.jsx, Alerts.jsx

Run from /opt/printfarm-scheduler/
    python3 deploy_v0170_alerts_phase2.py
"""
import os
import shutil

BASE_DIR = "/opt/printfarm-scheduler"
FRONTEND_SRC = os.path.join(BASE_DIR, "frontend/src")
COMPONENTS_DIR = os.path.join(FRONTEND_SRC, "components")
PAGES_DIR = os.path.join(FRONTEND_SRC, "pages")


def backup_file(filepath):
    bak = filepath + ".bak_v017"
    if not os.path.exists(bak):
        shutil.copy2(filepath, bak)
        print(f"  Backed up {os.path.basename(filepath)}")


def patch_api_js():
    """Add alert endpoints to api.js."""
    filepath = os.path.join(FRONTEND_SRC, "api.js")
    backup_file(filepath)

    with open(filepath, "r") as f:
        content = f.read()

    if "alertsApi" in content or "alerts/unread-count" in content:
        print("  api.js already has alert endpoints — skipping")
        return

    addition = """

// ============== Alerts & Notifications (v0.17.0) ==============

export const alerts = {
  list: (params = {}) => {
    const query = new URLSearchParams();
    if (params.severity) query.set('severity', params.severity);
    if (params.alert_type) query.set('alert_type', params.alert_type);
    if (params.is_read !== undefined) query.set('is_read', params.is_read);
    if (params.limit) query.set('limit', params.limit);
    if (params.offset) query.set('offset', params.offset);
    const qs = query.toString();
    return fetchAPI(`/alerts${qs ? '?' + qs : ''}`);
  },
  unreadCount: () => fetchAPI('/alerts/unread-count'),
  summary: () => fetchAPI('/alerts/summary'),
  markRead: (id) => fetchAPI(`/alerts/${id}/read`, { method: 'PATCH' }),
  markAllRead: () => fetchAPI('/alerts/mark-all-read', { method: 'POST' }),
  dismiss: (id) => fetchAPI(`/alerts/${id}/dismiss`, { method: 'PATCH' }),
};

export const alertPreferences = {
  get: () => fetchAPI('/alert-preferences'),
  update: (preferences) => fetchAPI('/alert-preferences', {
    method: 'PUT',
    body: JSON.stringify({ preferences }),
  }),
};

export const smtpConfig = {
  get: () => fetchAPI('/smtp-config'),
  update: (data) => fetchAPI('/smtp-config', {
    method: 'PUT',
    body: JSON.stringify(data),
  }),
  testEmail: () => fetchAPI('/alerts/test-email', { method: 'POST' }),
};

export const pushNotifications = {
  getVapidKey: () => fetchAPI('/push/vapid-key'),
  subscribe: (data) => fetchAPI('/push/subscribe', {
    method: 'POST',
    body: JSON.stringify(data),
  }),
  unsubscribe: () => fetchAPI('/push/subscribe', { method: 'DELETE' }),
};
"""

    with open(filepath, "a") as f:
        f.write(addition)

    print("  api.js patched with alert endpoints")


def patch_app_jsx():
    """Add AlertBell to header, Alerts nav item, and route."""
    filepath = os.path.join(FRONTEND_SRC, "App.jsx")
    backup_file(filepath)

    with open(filepath, "r") as f:
        content = f.read()

    if "AlertBell" in content:
        print("  App.jsx already has AlertBell — skipping")
        return

    # 1. Add imports
    content = content.replace(
        "import { useBranding } from './BrandingContext'",
        "import { useBranding } from './BrandingContext'\nimport AlertBell from './components/AlertBell'"
    )

    content = content.replace(
        "import Orders from './pages/Orders'",
        "import Orders from './pages/Orders'\nimport Alerts from './pages/Alerts'"
    )

    # Add Bell icon to lucide imports
    content = content.replace(
        "  ShoppingCart,\n} from 'lucide-react'",
        "  ShoppingCart,\n  Bell as BellIcon,\n} from 'lucide-react'"
    )

    # 2. Add AlertBell to desktop header (next to GlobalSearch)
    content = content.replace(
        '          <div className="hidden md:flex items-center justify-end p-4 border-b border-farm-800" style={{ backgroundColor: \'var(--brand-content-bg)\' }}>\n            <GlobalSearch />',
        '          <div className="hidden md:flex items-center justify-end gap-3 p-4 border-b border-farm-800" style={{ backgroundColor: \'var(--brand-content-bg)\' }}>\n            <GlobalSearch />\n            <AlertBell />'
    )

    # 3. Add AlertBell to mobile header (before hamburger)
    content = content.replace(
        '        <GlobalSearch />\n        <button \n          onClick={onMenuClick}',
        '        <GlobalSearch />\n        <AlertBell />\n        <button \n          onClick={onMenuClick}'
    )

    # 4. Add Alerts nav item in Work section (after Orders)
    content = content.replace(
        """{canAccessPage('jobs') && <NavItem collapsed={collapsed && !mobileOpen} to="/orders" icon={ShoppingCart} onClick={handleNavClick}>Orders</NavItem>}
          </>}""",
        """{canAccessPage('jobs') && <NavItem collapsed={collapsed && !mobileOpen} to="/orders" icon={ShoppingCart} onClick={handleNavClick}>Orders</NavItem>}
            <NavItem collapsed={collapsed && !mobileOpen} to="/alerts" icon={BellIcon} onClick={handleNavClick}>Alerts</NavItem>
          </>}"""
    )

    # 5. Add route
    content = content.replace(
        '<Route path="/orders" element={<Orders />} />',
        '<Route path="/orders" element={<Orders />} />\n            <Route path="/alerts" element={<Alerts />} />'
    )

    with open(filepath, "w") as f:
        f.write(content)

    print("  App.jsx patched with AlertBell, Alerts nav item, and route")


def create_alert_bell():
    """Create AlertBell.jsx component."""
    filepath = os.path.join(COMPONENTS_DIR, "AlertBell.jsx")
    if os.path.exists(filepath):
        print("  AlertBell.jsx already exists — skipping")
        return

    content = r"""import { useState, useEffect, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { Bell } from 'lucide-react'
import { alerts as alertsApi } from '../api'

const SEVERITY_STYLES = {
  critical: { dot: 'bg-red-500', text: 'text-red-400', icon: '\u{1F534}' },
  warning: { dot: 'bg-amber-500', text: 'text-amber-400', icon: '\u{1F7E1}' },
  info: { dot: 'bg-green-500', text: 'text-green-400', icon: '\u{1F7E2}' },
}

function timeAgo(dateStr) {
  const now = new Date()
  const date = new Date(dateStr)
  const seconds = Math.floor((now - date) / 1000)
  if (seconds < 60) return 'just now'
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  return `${days}d ago`
}

export default function AlertBell() {
  const [unreadCount, setUnreadCount] = useState(0)
  const [recentAlerts, setRecentAlerts] = useState([])
  const [isOpen, setIsOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const dropdownRef = useRef(null)
  const navigate = useNavigate()

  useEffect(() => {
    const fetchCount = async () => {
      try {
        const data = await alertsApi.unreadCount()
        setUnreadCount(data.unread_count)
      } catch (err) {}
    }
    fetchCount()
    const interval = setInterval(fetchCount, 10000)
    return () => clearInterval(interval)
  }, [])

  useEffect(() => {
    if (isOpen) {
      setLoading(true)
      alertsApi.list({ limit: 10 })
        .then(data => setRecentAlerts(data))
        .catch(() => {})
        .finally(() => setLoading(false))
    }
  }, [isOpen])

  useEffect(() => {
    const handleClickOutside = (e) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target)) {
        setIsOpen(false)
      }
    }
    if (isOpen) document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [isOpen])

  const handleAlertClick = async (alert) => {
    try {
      if (!alert.is_read) {
        await alertsApi.markRead(alert.id)
        setUnreadCount(prev => Math.max(0, prev - 1))
        setRecentAlerts(prev => prev.map(a => a.id === alert.id ? { ...a, is_read: true } : a))
      }
    } catch (err) {}
    setIsOpen(false)
    if (alert.job_id) navigate('/jobs')
    else if (alert.spool_id) navigate('/spools')
    else if (alert.printer_id) navigate('/printers')
    else navigate('/alerts')
  }

  const handleMarkAllRead = async () => {
    try {
      await alertsApi.markAllRead()
      setUnreadCount(0)
      setRecentAlerts(prev => prev.map(a => ({ ...a, is_read: true })))
    } catch (err) {}
  }

  return (
    <div className="relative" ref={dropdownRef}>
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="relative p-2 rounded-lg transition-colors hover:bg-farm-800"
        style={{ color: 'var(--brand-text-secondary)' }}
      >
        <Bell size={20} />
        {unreadCount > 0 && (
          <span className="absolute -top-0.5 -right-0.5 min-w-[18px] h-[18px] flex items-center justify-center bg-red-500 text-white text-[10px] font-bold rounded-full px-1">
            {unreadCount > 99 ? '99+' : unreadCount}
          </span>
        )}
      </button>

      {isOpen && (
        <div
          className="absolute right-0 top-full mt-2 w-80 rounded-xl border shadow-xl z-50 overflow-hidden"
          style={{
            backgroundColor: 'var(--brand-card-bg)',
            borderColor: 'var(--brand-sidebar-border)',
          }}
        >
          <div className="flex items-center justify-between px-4 py-3" style={{ borderBottom: '1px solid var(--brand-sidebar-border)' }}>
            <span className="text-sm font-semibold" style={{ color: 'var(--brand-text-primary)' }}>Notifications</span>
            {unreadCount > 0 && (
              <button
                onClick={handleMarkAllRead}
                className="text-xs transition-colors"
                style={{ color: 'var(--brand-accent)' }}
              >
                Mark all read
              </button>
            )}
          </div>

          <div className="max-h-80 overflow-y-auto">
            {loading ? (
              <div className="p-4 text-center text-sm" style={{ color: 'var(--brand-text-muted)' }}>Loading...</div>
            ) : recentAlerts.length === 0 ? (
              <div className="p-4 text-center text-sm" style={{ color: 'var(--brand-text-muted)' }}>No notifications</div>
            ) : (
              recentAlerts.map(alert => {
                const sev = SEVERITY_STYLES[alert.severity] || SEVERITY_STYLES.info
                return (
                  <button
                    key={alert.id}
                    onClick={() => handleAlertClick(alert)}
                    className="w-full text-left px-4 py-3 transition-colors hover:bg-farm-800/50"
                    style={{
                      borderBottom: '1px solid var(--brand-sidebar-border)',
                      opacity: alert.is_read ? 0.6 : 1,
                    }}
                  >
                    <div className="flex gap-2">
                      <span className="text-sm flex-shrink-0 mt-0.5">{sev.icon}</span>
                      <div className="min-w-0 flex-1">
                        <div className={`text-sm font-medium truncate ${!alert.is_read ? 'text-white' : ''}`} style={alert.is_read ? { color: 'var(--brand-text-secondary)' } : {}}>
                          {alert.title}
                        </div>
                        {alert.message && (
                          <div className="text-xs mt-0.5 truncate" style={{ color: 'var(--brand-text-muted)' }}>
                            {alert.message}
                          </div>
                        )}
                        <div className="text-[10px] mt-1" style={{ color: 'var(--brand-text-muted)' }}>
                          {timeAgo(alert.created_at)}
                        </div>
                      </div>
                      {!alert.is_read && <span className="w-2 h-2 rounded-full bg-blue-500 flex-shrink-0 mt-2" />}
                    </div>
                  </button>
                )
              })
            )}
          </div>

          <div className="px-4 py-2.5" style={{ borderTop: '1px solid var(--brand-sidebar-border)' }}>
            <button
              onClick={() => { setIsOpen(false); navigate('/alerts') }}
              className="text-xs font-medium transition-colors w-full text-center"
              style={{ color: 'var(--brand-accent)' }}
            >
              View all alerts
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
"""

    with open(filepath, "w") as f:
        f.write(content)

    print("  AlertBell.jsx created")


def create_alerts_page():
    """Create Alerts.jsx page."""
    filepath = os.path.join(PAGES_DIR, "Alerts.jsx")
    if os.path.exists(filepath):
        print("  Alerts.jsx already exists — skipping")
        return

    content = r"""import { useState, useEffect } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Bell, Check, X, ExternalLink } from 'lucide-react'
import { alerts as alertsApi } from '../api'

const SEVERITY_STYLES = {
  critical: { bg: 'border-l-red-500', icon: '\u{1F534}', label: 'Critical' },
  warning: { bg: 'border-l-amber-500', icon: '\u{1F7E1}', label: 'Warning' },
  info: { bg: 'border-l-green-500', icon: '\u{1F7E2}', label: 'Info' },
}

const TABS = [
  { key: 'all', label: 'All' },
  { key: 'unread', label: 'Unread' },
  { key: 'critical', label: 'Critical' },
  { key: 'warning', label: 'Warning' },
  { key: 'info', label: 'Info' },
]

function formatDate(dateStr) {
  const d = new Date(dateStr)
  return d.toLocaleDateString('en-US', {
    month: 'short', day: 'numeric', year: 'numeric',
    hour: 'numeric', minute: '2-digit',
  })
}

export default function Alerts() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [alerts, setAlerts] = useState([])
  const [loading, setLoading] = useState(true)
  const [activeTab, setActiveTab] = useState(searchParams.get('filter') || 'all')
  const [hasMore, setHasMore] = useState(false)
  const [offset, setOffset] = useState(0)
  const LIMIT = 25

  const loadAlerts = async (reset = false) => {
    try {
      setLoading(true)
      const newOffset = reset ? 0 : offset
      const params = { limit: LIMIT, offset: newOffset }

      if (activeTab === 'unread') {
        params.is_read = false
      } else if (['critical', 'warning', 'info'].includes(activeTab)) {
        params.severity = activeTab
      }

      const data = await alertsApi.list(params)
      if (reset) {
        setAlerts(data)
        setOffset(LIMIT)
      } else {
        setAlerts(prev => [...prev, ...data])
        setOffset(prev => prev + LIMIT)
      }
      setHasMore(data.length === LIMIT)
    } catch (err) {
      console.error('Failed to load alerts:', err)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadAlerts(true)
  }, [activeTab])

  const handleTabChange = (tab) => {
    setActiveTab(tab)
    setSearchParams(tab === 'all' ? {} : { filter: tab })
  }

  const handleMarkRead = async (alertId) => {
    try {
      await alertsApi.markRead(alertId)
      setAlerts(prev => prev.map(a => a.id === alertId ? { ...a, is_read: true } : a))
    } catch (err) {}
  }

  const handleDismiss = async (alertId) => {
    try {
      await alertsApi.dismiss(alertId)
      setAlerts(prev => prev.filter(a => a.id !== alertId))
    } catch (err) {}
  }

  const handleMarkAllRead = async () => {
    try {
      await alertsApi.markAllRead()
      setAlerts(prev => prev.map(a => ({ ...a, is_read: true })))
    } catch (err) {}
  }

  const getActionLink = (alert) => {
    if (alert.job_id) return { label: 'View Job', path: '/jobs' }
    if (alert.spool_id) return { label: 'View Spool', path: '/spools' }
    if (alert.printer_id) return { label: 'View Printer', path: '/printers' }
    if (alert.alert_type === 'maintenance_overdue') return { label: 'View Maintenance', path: '/maintenance' }
    return null
  }

  const unreadCount = alerts.filter(a => !a.is_read).length

  return (
    <div className="p-6 max-w-4xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-3">
          <Bell size={24} style={{ color: 'var(--brand-accent)' }} />
          <h1 className="text-2xl font-bold" style={{ color: 'var(--brand-text-primary)' }}>Alerts</h1>
          {unreadCount > 0 && (
            <span className="bg-red-500/20 text-red-400 text-xs font-medium px-2 py-0.5 rounded-full">
              {unreadCount} unread
            </span>
          )}
        </div>
        {unreadCount > 0 && (
          <button
            onClick={handleMarkAllRead}
            className="px-3 py-1.5 text-xs rounded-lg transition-colors"
            style={{ backgroundColor: 'var(--brand-accent)', color: 'white' }}
          >
            Mark all read
          </button>
        )}
      </div>

      <div className="flex gap-1 mb-6 p-1 rounded-lg" style={{ backgroundColor: 'var(--brand-sidebar-bg)' }}>
        {TABS.map(tab => (
          <button
            key={tab.key}
            onClick={() => handleTabChange(tab.key)}
            className="px-3 py-1.5 text-xs font-medium rounded-md transition-colors"
            style={activeTab === tab.key
              ? { backgroundColor: 'var(--brand-accent)', color: 'white' }
              : { color: 'var(--brand-text-muted)' }
            }
          >
            {tab.label}
          </button>
        ))}
      </div>

      <div className="space-y-3">
        {loading && alerts.length === 0 ? (
          <div className="text-center py-12" style={{ color: 'var(--brand-text-muted)' }}>Loading alerts...</div>
        ) : alerts.length === 0 ? (
          <div className="text-center py-12 rounded-xl border" style={{
            backgroundColor: 'var(--brand-card-bg)',
            borderColor: 'var(--brand-sidebar-border)',
            color: 'var(--brand-text-muted)',
          }}>
            <Bell size={32} className="mx-auto mb-3 opacity-30" />
            <p className="text-sm">No alerts to show</p>
          </div>
        ) : (
          alerts.map(alert => {
            const sev = SEVERITY_STYLES[alert.severity] || SEVERITY_STYLES.info
            const action = getActionLink(alert)

            return (
              <div
                key={alert.id}
                className={`rounded-xl border border-l-4 ${sev.bg} transition-all ${!alert.is_read ? 'ring-1 ring-blue-500/20' : ''}`}
                style={{
                  backgroundColor: 'var(--brand-card-bg)',
                  borderColor: 'var(--brand-sidebar-border)',
                  opacity: alert.is_read ? 0.7 : 1,
                }}
              >
                <div className="p-4">
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex gap-3 min-w-0 flex-1">
                      <span className="text-lg flex-shrink-0">{sev.icon}</span>
                      <div className="min-w-0 flex-1">
                        <div className="text-sm font-semibold" style={{ color: 'var(--brand-text-primary)' }}>
                          {alert.title}
                        </div>
                        {alert.message && (
                          <p className="text-xs mt-1 leading-relaxed" style={{ color: 'var(--brand-text-secondary)' }}>
                            {alert.message}
                          </p>
                        )}
                        <div className="flex items-center gap-3 mt-2">
                          <span className="text-[10px]" style={{ color: 'var(--brand-text-muted)' }}>
                            {formatDate(alert.created_at)}
                          </span>
                          {alert.printer_name && (
                            <span className="text-[10px] px-1.5 py-0.5 rounded" style={{
                              backgroundColor: 'var(--brand-sidebar-bg)',
                              color: 'var(--brand-text-muted)',
                            }}>
                              {alert.printer_name}
                            </span>
                          )}
                        </div>
                      </div>
                    </div>

                    <div className="flex items-center gap-1 flex-shrink-0">
                      {action && (
                        <a
                          href={action.path}
                          className="p-1.5 rounded transition-colors hover:bg-farm-800 text-farm-400"
                          title={action.label}
                        >
                          <ExternalLink size={14} />
                        </a>
                      )}
                      {!alert.is_read && (
                        <button
                          onClick={() => handleMarkRead(alert.id)}
                          className="p-1.5 rounded transition-colors hover:bg-farm-800 text-farm-400"
                          title="Mark read"
                        >
                          <Check size={14} />
                        </button>
                      )}
                      <button
                        onClick={() => handleDismiss(alert.id)}
                        className="p-1.5 rounded transition-colors hover:bg-red-900/50 text-farm-500 hover:text-red-400"
                        title="Dismiss"
                      >
                        <X size={14} />
                      </button>
                    </div>
                  </div>
                </div>
              </div>
            )
          })
        )}
      </div>

      {hasMore && (
        <div className="text-center mt-6">
          <button
            onClick={() => loadAlerts(false)}
            className="px-4 py-2 text-sm rounded-lg transition-colors"
            style={{ backgroundColor: 'var(--brand-sidebar-bg)', color: 'var(--brand-text-secondary)' }}
            disabled={loading}
          >
            {loading ? 'Loading...' : 'Load more...'}
          </button>
        </div>
      )}
    </div>
  )
}
"""

    with open(filepath, "w") as f:
        f.write(content)

    print("  Alerts.jsx created")


def main():
    print("=" * 60)
    print("v0.17.0 Phase 2 — Frontend Alerts UI")
    print("=" * 60)
    print()

    print("[1/4] Creating AlertBell.jsx component...")
    create_alert_bell()

    print("[2/4] Creating Alerts.jsx page...")
    create_alerts_page()

    print("[3/4] Patching api.js...")
    patch_api_js()

    print("[4/4] Patching App.jsx...")
    patch_app_jsx()

    print()
    print("=" * 60)
    print("Done! Next steps:")
    print("  1. Frontend auto-rebuilds (Vite dev server)")
    print("  2. Or restart: systemctl restart printfarm-frontend")
    print("  3. Check bell icon in top-right header bar")
    print("  4. Click 'Alerts' in sidebar under WORK")
    print("=" * 60)


if __name__ == "__main__":
    main()
