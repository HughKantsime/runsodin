// Self-hosted fonts (GDPR — no Google Fonts CDN)
import '@fontsource/inter/400.css'
import '@fontsource/inter/500.css'
import '@fontsource/inter/600.css'
import '@fontsource/inter/700.css'
import '@fontsource/jetbrains-mono/400.css'
import '@fontsource/jetbrains-mono/500.css'
import '@fontsource/space-grotesk/500.css'
import '@fontsource/space-grotesk/600.css'
import '@fontsource/space-grotesk/700.css'
import '@fontsource/ibm-plex-mono/400.css'
import '@fontsource/ibm-plex-mono/400-italic.css'
import '@fontsource/ibm-plex-mono/500.css'
import '@fontsource/ibm-plex-mono/600.css'
import '@fontsource/ibm-plex-mono/700.css'
import '@fontsource/ibm-plex-sans/300.css'
import '@fontsource/ibm-plex-sans/400.css'
import '@fontsource/ibm-plex-sans/500.css'
import '@fontsource/ibm-plex-sans/600.css'
import '@fontsource/ibm-plex-sans/700.css'

import { useState, useEffect } from "react"
import { canAccessPage, canDo, getCurrentUser } from './permissions'
import { Routes, Route, NavLink, Navigate, useLocation, Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Toaster } from 'react-hot-toast'
import {
  LayoutDashboard,
  Video,
  Calendar,
  Printer,
  Box,
  ListTodo,
  Settings,
  Activity,
  BarChart3,
  Calculator,
  Upload as UploadIcon,
  LogOut,
  ChevronLeft,
  ChevronRight,
  Menu,
  X,
  ChevronDown,
  Wrench,
  ShoppingCart,
  Bell as BellIcon,
  ShoppingBag,
  Circle,
  Sun,
  Moon,
  Package,
  Eye,
  FileText,
  Film,
  Archive,
  SlidersHorizontal,
  ClipboardList,
  FolderKanban,
} from 'lucide-react'
import clsx from 'clsx'
import { useBranding } from './BrandingContext'
import { useLicense } from './LicenseContext'
import { useOrg } from './contexts/OrgContext'
import ProGate from './components/shared/ProGate'
import ProBadge from './components/shared/ProBadge'
import AlertBell from './components/notifications/AlertBell'
import EmergencyStop from './components/printers/EmergencyStop'
import UpgradeBanner from './components/shared/UpgradeBanner'
import GlobalSearch from './components/shared/GlobalSearch'
import ErrorBoundary from './components/shared/ErrorBoundary'
import Dashboard from './pages/dashboard/Dashboard'
import Timeline from './pages/jobs/Timeline'
import Jobs from './pages/jobs/Jobs'
import Printers from './pages/printers/Printers'
import Models from './pages/models/Models'
import CalculatorPage from './pages/orders/Calculator'
import Analytics from './pages/analytics/Analytics'
import Utilization from './pages/analytics/Utilization'
import SettingsPage from './pages/admin/Settings'
import Spools from './pages/inventory/Spools'
import Consumables from './pages/inventory/Consumables'
import Upload from './pages/jobs/Upload'
import Login from './pages/auth/Login'
import Setup from './pages/auth/Setup'
import Maintenance from './pages/printers/Maintenance'
import Cameras from "./pages/printers/Cameras"
import CameraDetail from "./pages/printers/CameraDetail"
import TVDashboard from "./pages/dashboard/TVDashboard"
import Products from './pages/orders/Products'
import Orders from './pages/orders/Orders'
import Alerts from './pages/notifications/Alerts'
import Detections from './pages/vision/Detections'
import EducationReports from './pages/analytics/EducationReports'
import AuditLogs from './pages/admin/AuditLogs'
import Timelapses from './pages/archives/Timelapses'
import Overlay from './pages/dashboard/Overlay'
import ResetPassword from './pages/auth/ResetPassword'
import ArchivesPage from './pages/archives/Archives'
import PrintLog from './pages/analytics/PrintLog'
import ProjectsPage from './pages/models/Projects'
import Profiles from './pages/models/Profiles'
import { stats, printers, getEducationMode, pricingConfig, setup } from './api'
import useWebSocket from './hooks/useWebSocket'
import useKeyboardShortcuts from './hooks/useKeyboardShortcuts'
import KeyboardShortcutsModal from './components/shared/KeyboardShortcutsModal'


function NavItem({ to, icon: Icon, children, collapsed, onClick }) {
  return (
    <NavLink 
      to={to}
      onClick={onClick}
      className={({ isActive }) => clsx(
        'transition-colors border-l-3',
        collapsed ? 'flex items-center justify-center py-2 rounded-lg' 
                  : 'flex items-center gap-3 px-4 py-2 rounded-lg text-sm',
        isActive ? 'border-l-print-500' : 'border-l-transparent',
      )}
      style={({ isActive }) => isActive
        ? { backgroundColor: 'var(--brand-sidebar-active-bg)', color: 'var(--brand-sidebar-active-text)' }
        : { color: 'var(--brand-sidebar-text)' }
      }
    >
      <Icon size={18} className="flex-shrink-0" />
      {!collapsed && <span className="font-medium">{children}</span>}
    </NavLink>
  )
}


function NavGroup({ label, collapsed, open, onToggle }) {
  return (
    <div className="pt-4 pb-1">
      <div style={{ borderTop: '1px solid var(--brand-sidebar-border)' }} />
      {!collapsed && (
        <button
          onClick={onToggle}
          className="flex items-center justify-between w-full px-4 mt-2 group"
        >
          <span className="text-[9px] uppercase font-mono font-medium" 
            style={{ color: 'var(--brand-text-muted)', letterSpacing: '0.2em' }}>
            {label}
          </span>
          <ChevronDown
            size={12}
            className={clsx("transition-transform duration-200", open ? "" : "-rotate-90")}
            style={{ color: 'var(--brand-text-muted)' }}
          />
        </button>
      )}
    </div>
  )
}


function Sidebar({ mobileOpen, onMobileClose }) {
  const [collapsed, setCollapsed] = useState(false)
  const [sections, setSections] = useState({ work: true, library: true, monitor: true, tools: true })
  const [uiMode, setUiMode] = useState('advanced')
  const [educationMode, setEducationMode] = useState(false)
  useEffect(() => {
    pricingConfig.get()
      .then(d => { if (d.ui_mode) setUiMode(d.ui_mode) }).catch(() => {})
    const handler = (e) => setUiMode(e.detail)
    window.addEventListener('ui-mode-changed', handler)
    return () => window.removeEventListener('ui-mode-changed', handler)
  }, [])
  useEffect(() => {
    getEducationMode().then(d => setEducationMode(d?.enabled || false)).catch(() => {})
    const handler = () => getEducationMode().then(d => setEducationMode(d?.enabled || false)).catch(() => {})
    window.addEventListener('education-mode-changed', handler)
    return () => window.removeEventListener('education-mode-changed', handler)
  }, [])
  const adv = uiMode === 'advanced'
  const lic = useLicense()
  const org = useOrg()
  const toggle = (key) => setSections(s => ({ ...s, [key]: !s[key] }))
  const branding = useBranding()

  const { data: statsData } = useQuery({
    queryKey: ['stats'],
    queryFn: stats.get,
    refetchInterval: 30000,
  })
  const { data: printersData } = useQuery({
    queryKey: ['sidebar-printers', org.orgId],
    queryFn: () => printers.list(false, '', org.orgId),
    refetchInterval: 15000,
  })

  // Close mobile menu on nav click
  const handleNavClick = () => {
    if (mobileOpen && onMobileClose) {
      onMobileClose()
    }
  }

  return (
    <>
      {/* Mobile overlay */}
      {mobileOpen && (
        <div 
          className="fixed inset-0 bg-black/50 z-40 md:hidden"
          onClick={onMobileClose}
        />
      )}
      
      <aside
        role="navigation"
        aria-label="Main navigation"
        className={clsx(
          "flex flex-col h-full transition-all duration-300",
          // Mobile: fixed drawer, hidden by default
          "fixed md:relative z-50 md:z-auto",
          mobileOpen ? "translate-x-0" : "-translate-x-full md:translate-x-0",
          // Width
          collapsed ? "w-64 md:w-16" : "w-64"
        )}
        style={{
          backgroundColor: 'var(--brand-sidebar-bg)',
          borderRight: '1px solid var(--brand-sidebar-border)',
        }}
      >
        {/* Logo */}
        <div 
          className={clsx("flex items-center", collapsed && !mobileOpen ? "p-3 justify-center" : "p-6 justify-between")}
          style={{ borderBottom: '1px solid var(--brand-sidebar-border)' }}
        >
          <div>
            {(!collapsed || mobileOpen) && (
              <div className="flex items-center gap-3">
                {branding.logo_url && (
                  <img src={branding.logo_url} alt={branding.app_name} className="h-8 object-contain" />
                )}
                <div>
                  <h1 className="text-base font-display font-bold leading-tight" style={{ color: 'var(--brand-text-primary)' }}>
                    {branding.app_name}
                  </h1>
                  <p className="text-xs" style={{ color: 'var(--brand-text-muted)' }}>
                    {branding.app_subtitle}
                  </p>
                </div>
              </div>
            )}
          </div>
          {/* Desktop collapse button */}
          <button
            onClick={() => setCollapsed(!collapsed)}
            className="hidden md:block transition-colors"
            style={{ color: 'var(--brand-sidebar-text)' }}
            aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          >
            {collapsed ? <ChevronRight size={20} /> : <ChevronLeft size={20} />}
          </button>
          {/* Mobile close button */}
          <button
            onClick={onMobileClose}
            className="md:hidden transition-colors"
            style={{ color: 'var(--brand-sidebar-text)' }}
            aria-label="Close menu"
          >
            <X size={20} />
          </button>
        </div>

        {/* Org Switcher (superadmin only) */}
        {org.isAdmin && org.orgs.length > 0 && (!collapsed || mobileOpen) && (
          <div className="px-4 py-2" style={{ borderBottom: '1px solid var(--brand-sidebar-border)' }}>
            <select
              value={org.orgId || ''}
              onChange={(e) => org.switchOrg(e.target.value ? parseInt(e.target.value) : null)}
              className="w-full bg-farm-800 border border-farm-700 rounded-lg px-2 py-1.5 text-xs"
              style={{ color: 'var(--brand-sidebar-text)' }}
            >
              <option value="">All Organizations</option>
              {org.orgs.map(o => (
                <option key={o.id} value={o.id}>{o.name}</option>
              ))}
            </select>
          </div>
        )}

        {/* Navigation */}
        <nav className="flex-1 overflow-y-auto p-4 space-y-1 min-h-0">
          {/* Monitor */}
          {canAccessPage('dashboard') && <NavItem collapsed={collapsed && !mobileOpen} to="/" icon={LayoutDashboard} onClick={handleNavClick}>Dashboard</NavItem>}
          {canAccessPage('printers') && <NavItem collapsed={collapsed && !mobileOpen} to="/printers" icon={Printer} onClick={handleNavClick}>Printers</NavItem>}
          {canAccessPage('cameras') && <NavItem collapsed={collapsed && !mobileOpen} to="/cameras" icon={Video} onClick={handleNavClick}>Cameras</NavItem>}
          {canAccessPage('cameras') && <NavItem collapsed={collapsed && !mobileOpen} to="/timelapses" icon={Film} onClick={handleNavClick}>Timelapses</NavItem>}
          <NavItem collapsed={collapsed && !mobileOpen} to="/archives" icon={Archive} onClick={handleNavClick}>Archives</NavItem>
          <NavItem collapsed={collapsed && !mobileOpen} to="/projects" icon={FolderKanban} onClick={handleNavClick}>Projects</NavItem>
          <NavItem collapsed={collapsed && !mobileOpen} to="/print-log" icon={ClipboardList} onClick={handleNavClick}>Print Log</NavItem>
          {canAccessPage('timeline') && <NavItem collapsed={collapsed && !mobileOpen} to="/timeline" icon={Calendar} onClick={handleNavClick}>Timeline</NavItem>}

          {/* Work */}
          {(canAccessPage("jobs") || canAccessPage("upload")) && <NavGroup label="Work" collapsed={collapsed && !mobileOpen} open={sections.work} onToggle={() => toggle("work")} />}
          {((collapsed && !mobileOpen) || sections.work) && <>
            {canAccessPage('jobs') && <NavItem collapsed={collapsed && !mobileOpen} to="/jobs" icon={ListTodo} onClick={handleNavClick}>Jobs</NavItem>}
            {adv && lic.isPro && !educationMode && canAccessPage('jobs') && <NavItem collapsed={collapsed && !mobileOpen} to="/orders" icon={ShoppingCart} onClick={handleNavClick}>Orders{!lic.isPro && <ProBadge />}</NavItem>}
            {canAccessPage('upload') && <NavItem collapsed={collapsed && !mobileOpen} to="/upload" icon={UploadIcon} onClick={handleNavClick}>Upload</NavItem>}
          </>}

          {/* Library */}
          {(canAccessPage("models") || canAccessPage("spools")) && <NavGroup label="Library" collapsed={collapsed && !mobileOpen} open={sections.library} onToggle={() => toggle("library")} />}
          {((collapsed && !mobileOpen) || sections.library) && <>
            {canAccessPage('models') && <NavItem collapsed={collapsed && !mobileOpen} to="/models" icon={Box} onClick={handleNavClick}>Models</NavItem>}
            <NavItem collapsed={collapsed && !mobileOpen} to="/profiles" icon={SlidersHorizontal} onClick={handleNavClick}>Profiles</NavItem>
            {adv && lic.isPro && !educationMode && canAccessPage('models') && <NavItem collapsed={collapsed && !mobileOpen} to="/products" icon={ShoppingBag} onClick={handleNavClick}>Products{!lic.isPro && <ProBadge />}</NavItem>}
            {canAccessPage('spools') && <NavItem collapsed={collapsed && !mobileOpen} to="/spools" icon={Circle} onClick={handleNavClick}>Spools</NavItem>}
            {adv && lic.isPro && !educationMode && canAccessPage('models') && <NavItem collapsed={collapsed && !mobileOpen} to="/consumables" icon={Package} onClick={handleNavClick}>Consumables{!lic.isPro && <ProBadge />}</NavItem>}
          </>}

          {/* Monitor */}
          {adv && lic.isPro && (canAccessPage("analytics") || canAccessPage("maintenance")) && <NavGroup label="Monitor" collapsed={collapsed && !mobileOpen} open={sections.monitor} onToggle={() => toggle("monitor")} />}
          {adv && lic.isPro && ((collapsed && !mobileOpen) || sections.monitor) && <>
            <NavItem collapsed={collapsed && !mobileOpen} to="/alerts" icon={BellIcon} onClick={handleNavClick}>Alerts</NavItem>
            <NavItem collapsed={collapsed && !mobileOpen} to="/detections" icon={Eye} onClick={handleNavClick}>Detections</NavItem>
            {lic.isPro && canAccessPage('maintenance') && <NavItem collapsed={collapsed && !mobileOpen} to="/maintenance" icon={Wrench} onClick={handleNavClick}>Maintenance{!lic.isPro && <ProBadge />}</NavItem>}
            {lic.isPro && canAccessPage('analytics') && <NavItem collapsed={collapsed && !mobileOpen} to="/analytics" icon={BarChart3} onClick={handleNavClick}>Analytics{!lic.isPro && <ProBadge />}</NavItem>}
            {lic.isPro && canAccessPage('analytics') && <NavItem collapsed={collapsed && !mobileOpen} to="/utilization" icon={Activity} onClick={handleNavClick}>Utilization{!lic.isPro && <ProBadge />}</NavItem>}
            {lic.isPro && canAccessPage('education_reports') && <NavItem collapsed={collapsed && !mobileOpen} to="/education-reports" icon={BarChart3} onClick={handleNavClick}>Usage Reports</NavItem>}
          </>}

          {/* Tools */}
          {adv && canAccessPage("calculator") && <NavGroup label="Tools" collapsed={collapsed && !mobileOpen} open={sections.tools} onToggle={() => toggle("tools")} />}
          {adv && ((collapsed && !mobileOpen) || sections.tools) && <>
            {canAccessPage('calculator') && <NavItem collapsed={collapsed && !mobileOpen} to="/calculator" icon={Calculator} onClick={handleNavClick}>Calculator</NavItem>}
          </>}

          {/* Settings */}
          <div className="mt-2" style={{ borderTop: '1px solid var(--brand-sidebar-border)', paddingTop: '0.5rem' }}>
            {canAccessPage('settings') && <NavItem collapsed={collapsed && !mobileOpen} to="/settings" icon={Settings} onClick={handleNavClick}>Settings</NavItem>}
            {canAccessPage('audit') && <NavItem collapsed={collapsed && !mobileOpen} to="/audit" icon={FileText} onClick={handleNavClick}>Audit Log</NavItem>}
          </div>
        </nav>

        {/* Fleet Status */}
        {printersData && (!collapsed || mobileOpen) && (
          <NavLink to="/printers" className="flex-shrink-0 px-4 py-3 hover:opacity-80 transition-opacity overflow-hidden" style={{ borderTop: '1px solid var(--brand-sidebar-border)' }} aria-label={`Fleet status: ${printersData.filter(p => p.last_seen && (Date.now() - new Date(p.last_seen + 'Z').getTime()) < 90000).length} of ${printersData.length} printers online`}>
            <div className="flex items-center gap-2 mb-1.5">
              <span className="text-xs font-medium whitespace-nowrap" style={{ color: 'var(--brand-sidebar-text)' }}>
                {printersData.filter(p => p.last_seen && (Date.now() - new Date(p.last_seen + 'Z').getTime()) < 90000).length}/{printersData.length} online
              </span>
            </div>
            <div className="flex flex-wrap gap-0.5" aria-hidden="true">
              {printersData.map(p => {
                const online = p.last_seen && (Date.now() - new Date(p.last_seen + 'Z').getTime()) < 90000
                return <div key={p.id} className={`w-2 h-2 rounded-full ${online ? 'bg-green-500' : 'bg-farm-600'}`} />
              })}
            </div>
          </NavLink>
        )}

        {/* Footer */}
        <div className="flex-shrink-0 px-4 py-3" style={{ borderTop: '1px solid var(--brand-sidebar-border)' }}>
          <div className="flex items-center justify-between">
            {(!collapsed || mobileOpen) && (
              <span className="text-[10px]" style={{ color: 'var(--brand-text-muted)' }}>v{__APP_VERSION__}</span>
            )}
            <button
              onClick={async () => {
                await fetch('/api/auth/logout', { method: 'POST', credentials: 'include' }).catch(() => {})
                window.location.href = "/login";
              }}
              className="flex items-center gap-2 hover:text-red-400 text-sm transition-colors"
              style={{ color: 'var(--brand-sidebar-text)' }}
              aria-label="Logout"
            >
              <LogOut size={14} />
              {(!collapsed || mobileOpen) && "Logout"}
            </button>
          </div>
          {(!collapsed || mobileOpen) && (
            <p className="text-[10px] text-center mt-1" style={{ color: 'var(--brand-text-muted)', opacity: 0.6 }}>
              Powered by O.D.I.N.
            </p>
          )}
        </div>
      </aside>
    </>
  )
}


function ThemeToggle() {
  const [theme, setTheme] = useState(() => localStorage.getItem('odin-theme') || 'dark')
  useEffect(() => {
    document.documentElement.classList.toggle("light", theme === "light")
    localStorage.setItem('odin-theme', theme)
  }, [theme])
  return (
    <button
      onClick={() => setTheme(t => t === 'dark' ? 'light' : 'dark')}
      className="p-2 rounded-lg hover:bg-farm-800 text-farm-400 hover:text-farm-200 transition-colors"
      aria-label={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
    >
      {theme === 'dark' ? <Sun size={18} /> : <Moon size={18} />}
    </button>
  )
}

function MobileHeader({ onMenuClick }) {
  const branding = useBranding()
  
  return (
    <header 
      className="md:hidden flex items-center justify-between p-4 flex-shrink-0"
      style={{ 
        backgroundColor: 'var(--brand-sidebar-bg)',
        borderBottom: '1px solid var(--brand-sidebar-border)',
      }}
    >
      <div className="flex items-center gap-3">
        {branding.logo_url && (
          <img src={branding.logo_url} alt={branding.app_name} className="h-6 object-contain" />
        )}
        <h1 className="text-sm font-display font-bold" style={{ color: 'var(--brand-text-primary)' }}>
          {branding.app_name}
        </h1>
      </div>
      <div className="flex items-center gap-2">
        <GlobalSearch />
        <ThemeToggle />
        <AlertBell />
        <button
          onClick={onMenuClick}
          className="p-2 rounded-lg transition-colors"
          style={{ color: 'var(--brand-sidebar-text)' }}
          aria-label="Open menu"
        >
          <Menu size={24} />
        </button>
      </div>
    </header>
  )
}


// Token validity is determined by the session cookie (httpOnly, not readable in JS).
// Use the /api/auth/me endpoint to verify authentication status.


function ProtectedRoute({ children }) {
  const location = useLocation()
  const [authChecked, setAuthChecked] = useState(false)
  const [authenticated, setAuthenticated] = useState(false)

  useEffect(() => {
    fetch('/api/auth/me', { credentials: 'include' })
      .then(r => {
        setAuthenticated(r.ok)
        setAuthChecked(true)
      })
      .catch(() => {
        setAuthenticated(false)
        setAuthChecked(true)
      })
  }, [location.pathname])

  if (!authChecked) return null  // brief flicker while checking; acceptable

  if (!authenticated) {
    return <Navigate to="/login" state={{ from: location }} replace />
  }
  return children
}

function RoleGate({ page, children }) {
  if (!canAccessPage(page)) {
    return <Navigate to="/" replace />
  }
  return children
}

function NotFound() {
  return (
    <div className="flex flex-col items-center justify-center min-h-[50vh] p-8 text-center">
      <h2 className="text-4xl font-bold text-farm-100 mb-2">404</h2>
      <p className="text-sm text-farm-400 mb-6">Page not found</p>
      <Link to="/" className="px-4 py-2 bg-print-600 hover:bg-print-500 text-white rounded-lg text-sm transition-colors">
        Back to Dashboard
      </Link>
    </div>
  )
}


export default function App() {

  useWebSocket()
  const { showHelp, setShowHelp } = useKeyboardShortcuts()

  const location = useLocation()
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false)

  // Close mobile menu on route change
  useEffect(() => {
    setMobileMenuOpen(false)
  }, [location.pathname])

  // Check if first-time setup is needed
  useEffect(() => {
    if (location.pathname === '/setup' || location.pathname === '/tv') return
    setup.status()
      .then(data => {
        if (data.needs_setup) {
          window.location.href = '/setup'
        }
      })
      .catch(() => {})
  }, [])

  // Show setup wizard without sidebar
  if (location.pathname === '/setup') {
    return <Setup />
  }

  // Show login page without sidebar
  if (location.pathname === '/login') {
    return <Login />
  }

  // Password reset — no auth, no sidebar
  if (location.pathname === '/reset-password') {
    return <ResetPassword />
  }

  // OBS streaming overlay — no auth, no sidebar
  if (location.pathname.startsWith('/overlay/')) {
    return <Routes><Route path="/overlay/:printerId" element={<Overlay />} /></Routes>
  }

  // TV dashboard mode — full viewport, no sidebar
  if (location.pathname === '/tv') {
    return <ProtectedRoute><TVDashboard /></ProtectedRoute>
  }

  return (
    <ProtectedRoute>
      <div className="h-screen flex flex-col md:flex-row overflow-hidden">
        {/* Skip to content — WCAG 2.4.1 */}
        <a href="#main-content" className="sr-only focus:not-sr-only focus:absolute focus:z-[100] focus:top-2 focus:left-2 focus:px-4 focus:py-2 focus:bg-print-600 focus:text-white focus:rounded-lg focus:text-sm">
          Skip to content
        </a>

        {/* Mobile header with hamburger */}
        <MobileHeader onMenuClick={() => setMobileMenuOpen(true)} />

        {/* Sidebar - hidden on mobile until opened */}
        <Sidebar
          mobileOpen={mobileMenuOpen}
          onMobileClose={() => setMobileMenuOpen(false)}
        />

        {/* Main content */}
        <div className="flex-1 flex flex-col overflow-hidden">
          {/* Desktop search header */}
          <div className="hidden md:flex items-center justify-end gap-3 px-4 py-2 border-b border-farm-800" role="toolbar" aria-label="Global actions" style={{ backgroundColor: 'var(--brand-content-bg)' }}>
            <GlobalSearch />
            <ThemeToggle />
            <AlertBell />
          </div>
          <UpgradeBanner />
          <main id="main-content" className="flex-1 overflow-auto" style={{ backgroundColor: 'var(--brand-content-bg)' }}>
          {/* Screen reader live region for async status announcements */}
          <div role="status" aria-live="polite" aria-atomic="true" className="sr-only" id="sr-status" />
          <ErrorBoundary>
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/timeline" element={<Timeline />} />
            <Route path="/jobs" element={<Jobs />} />
            <Route path="/printers" element={<Printers />} />
            <Route path="/models" element={<Models />} />
            <Route path="/profiles" element={<Profiles />} />
            <Route path="/calculator" element={<CalculatorPage />} />
            <Route path="/analytics" element={<ProGate feature="analytics"><Analytics /></ProGate>} />
            <Route path="/utilization" element={<ProGate feature="analytics"><Utilization /></ProGate>} />
            <Route path="/upload" element={<Upload />} />
            <Route path="/spools" element={<Spools />} />
            <Route path="/consumables" element={<ProGate feature="products"><Consumables /></ProGate>} />
            <Route path="/settings" element={<RoleGate page="settings"><SettingsPage /></RoleGate>} />
            <Route path="/admin" element={<Navigate to="/settings" replace />} />
            <Route path="/permissions" element={<ProGate feature="permissions"><Navigate to="/settings" replace /></ProGate>} />
            <Route path="/maintenance" element={<ProGate feature="maintenance"><Maintenance /></ProGate>} />
            <Route path="/cameras" element={<Cameras />} />
            <Route path="/cameras/:id" element={<CameraDetail />} />
            <Route path="/branding" element={<ProGate feature="branding"><Navigate to="/settings" replace /></ProGate>} />
            <Route path="/products" element={<ProGate feature="products"><Products /></ProGate>} />
            <Route path="/orders" element={<ProGate feature="orders"><Orders /></ProGate>} />
            <Route path="/alerts" element={<Alerts />} />
            <Route path="/detections" element={<Detections />} />
            <Route path="/education-reports" element={<ProGate feature="usage_reports" tier="Pro"><EducationReports /></ProGate>} />
            <Route path="/timelapses" element={<Timelapses />} />
            <Route path="/archives" element={<ArchivesPage />} />
            <Route path="/projects" element={<ProjectsPage />} />
            <Route path="/print-log" element={<PrintLog />} />
            <Route path="/audit" element={<RoleGate page="audit"><AuditLogs /></RoleGate>} />
            <Route path="*" element={<NotFound />} />
          </Routes>
          </ErrorBoundary>
          <Toaster position="top-right" toastOptions={{ style: { background: 'var(--brand-card-bg)', color: 'var(--brand-text-primary)', border: '1px solid var(--brand-card-border)' } }} />
      {showHelp && <KeyboardShortcutsModal onClose={() => setShowHelp(false)} />}
            <EmergencyStop />
            <div className="text-center py-4 text-[10px] text-farm-600 select-none">Powered by O.D.I.N.</div>
          </main>
        </div>
      </div>
    </ProtectedRoute>
  )
}
