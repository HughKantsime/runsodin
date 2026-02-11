import { useState, useEffect } from "react"
import { canAccessPage, canDo, getCurrentUser } from './permissions'
import { Routes, Route, NavLink, Navigate, useLocation } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
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
} from 'lucide-react'
import clsx from 'clsx'
import { useBranding } from './BrandingContext'
import { useLicense } from './LicenseContext'
import ProGate from './components/ProGate'
import ProBadge from './components/ProBadge'
import AlertBell from './components/AlertBell'
import EmergencyStop from './components/EmergencyStop'
import GlobalSearch from './components/GlobalSearch'
import Dashboard from './pages/Dashboard'
import Timeline from './pages/Timeline'
import Jobs from './pages/Jobs'
import Printers from './pages/Printers'
import Models from './pages/Models'
import CalculatorPage from './pages/Calculator'
import Analytics from './pages/Analytics'
import Utilization from './pages/Utilization'
import SettingsPage from './pages/Settings'
import Spools from './pages/Spools'
import Consumables from './pages/Consumables'
import Upload from './pages/Upload'
import Login from './pages/Login'
import Setup from './pages/Setup'
import Maintenance from './pages/Maintenance'
import Cameras from "./pages/Cameras"
import Products from './pages/Products'
import Orders from './pages/Orders'
import Alerts from './pages/Alerts'
import Detections from './pages/Detections'
import EducationReports from './pages/EducationReports'
import { stats, printers } from './api'
import useWebSocket from './hooks/useWebSocket'
import useKeyboardShortcuts from './hooks/useKeyboardShortcuts'
import KeyboardShortcutsModal from './components/KeyboardShortcutsModal'


function NavItem({ to, icon: Icon, children, collapsed, onClick }) {
  return (
    <NavLink 
      to={to}
      onClick={onClick}
      className={({ isActive }) => clsx(
        'transition-colors border-l-3',
        collapsed ? 'flex items-center justify-center py-2 rounded' 
                  : 'flex items-center gap-3 px-4 py-2 rounded text-sm',
        isActive ? 'border-l-amber-500' : 'border-l-transparent',
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
  useEffect(() => {
    const key = localStorage.getItem('pf_api_key') || ''
    fetch('/api/pricing-config', { headers: { 'X-API-Key': key } })
      .then(r => r.json()).then(d => { if (d.ui_mode) setUiMode(d.ui_mode) }).catch(() => {})
    const handler = (e) => setUiMode(e.detail)
    window.addEventListener('ui-mode-changed', handler)
    return () => window.removeEventListener('ui-mode-changed', handler)
  }, [])
  const adv = uiMode === 'advanced'
  const lic = useLicense()
  const toggle = (key) => setSections(s => ({ ...s, [key]: !s[key] }))
  const branding = useBranding()

  const { data: statsData } = useQuery({
    queryKey: ['stats'],
    queryFn: stats.get,
    refetchInterval: 30000,
  })
  const { data: printersData } = useQuery({
    queryKey: ['sidebar-printers'],
    queryFn: () => printers.list(),
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
          >
            {collapsed ? <ChevronRight size={20} /> : <ChevronLeft size={20} />}
          </button>
          {/* Mobile close button */}
          <button 
            onClick={onMobileClose}
            className="md:hidden transition-colors"
            style={{ color: 'var(--brand-sidebar-text)' }}
          >
            <X size={20} />
          </button>
        </div>

        {/* Navigation */}
        <nav className="flex-1 overflow-y-auto p-4 space-y-1 min-h-0">
          {/* Monitor */}
          {canAccessPage('dashboard') && <NavItem collapsed={collapsed && !mobileOpen} to="/" icon={LayoutDashboard} onClick={handleNavClick}>Dashboard</NavItem>}
          {canAccessPage('printers') && <NavItem collapsed={collapsed && !mobileOpen} to="/printers" icon={Printer} onClick={handleNavClick}>Printers</NavItem>}
          {canAccessPage('cameras') && <NavItem collapsed={collapsed && !mobileOpen} to="/cameras" icon={Video} onClick={handleNavClick}>Cameras</NavItem>}
          {canAccessPage('timeline') && <NavItem collapsed={collapsed && !mobileOpen} to="/timeline" icon={Calendar} onClick={handleNavClick}>Timeline</NavItem>}

          {/* Work */}
          {(canAccessPage("jobs") || canAccessPage("upload")) && <NavGroup label="Work" collapsed={collapsed && !mobileOpen} open={sections.work} onToggle={() => toggle("work")} />}
          {((collapsed && !mobileOpen) || sections.work) && <>
            {canAccessPage('jobs') && <NavItem collapsed={collapsed && !mobileOpen} to="/jobs" icon={ListTodo} onClick={handleNavClick}>Jobs</NavItem>}
            {adv && lic.isPro && canAccessPage('jobs') && <NavItem collapsed={collapsed && !mobileOpen} to="/orders" icon={ShoppingCart} onClick={handleNavClick}>Orders{!lic.isPro && <ProBadge />}</NavItem>}
            {canAccessPage('upload') && <NavItem collapsed={collapsed && !mobileOpen} to="/upload" icon={UploadIcon} onClick={handleNavClick}>Upload</NavItem>}
          </>}

          {/* Library */}
          {(canAccessPage("models") || canAccessPage("spools")) && <NavGroup label="Library" collapsed={collapsed && !mobileOpen} open={sections.library} onToggle={() => toggle("library")} />}
          {((collapsed && !mobileOpen) || sections.library) && <>
            {canAccessPage('models') && <NavItem collapsed={collapsed && !mobileOpen} to="/models" icon={Box} onClick={handleNavClick}>Models</NavItem>}
            {adv && lic.isPro && canAccessPage('models') && <NavItem collapsed={collapsed && !mobileOpen} to="/products" icon={ShoppingBag} onClick={handleNavClick}>Products{!lic.isPro && <ProBadge />}</NavItem>}
            {canAccessPage('spools') && <NavItem collapsed={collapsed && !mobileOpen} to="/spools" icon={Circle} onClick={handleNavClick}>Spools</NavItem>}
            {adv && lic.isPro && canAccessPage('models') && <NavItem collapsed={collapsed && !mobileOpen} to="/consumables" icon={Package} onClick={handleNavClick}>Consumables{!lic.isPro && <ProBadge />}</NavItem>}
          </>}

          {/* Monitor */}
          {adv && lic.isPro && (canAccessPage("analytics") || canAccessPage("maintenance")) && <NavGroup label="Monitor" collapsed={collapsed && !mobileOpen} open={sections.monitor} onToggle={() => toggle("monitor")} />}
          {adv && lic.isPro && ((collapsed && !mobileOpen) || sections.monitor) && <>
            <NavItem collapsed={collapsed && !mobileOpen} to="/alerts" icon={BellIcon} onClick={handleNavClick}>Alerts</NavItem>
            <NavItem collapsed={collapsed && !mobileOpen} to="/detections" icon={Eye} onClick={handleNavClick}>Detections</NavItem>
            {lic.isPro && canAccessPage('maintenance') && <NavItem collapsed={collapsed && !mobileOpen} to="/maintenance" icon={Wrench} onClick={handleNavClick}>Maintenance{!lic.isPro && <ProBadge />}</NavItem>}
            {lic.isPro && canAccessPage('analytics') && <NavItem collapsed={collapsed && !mobileOpen} to="/analytics" icon={BarChart3} onClick={handleNavClick}>Analytics{!lic.isPro && <ProBadge />}</NavItem>}
            {lic.isPro && canAccessPage('analytics') && <NavItem collapsed={collapsed && !mobileOpen} to="/utilization" icon={Activity} onClick={handleNavClick}>Utilization{!lic.isPro && <ProBadge />}</NavItem>}
            {lic.isEducation && canAccessPage('education_reports') && <NavItem collapsed={collapsed && !mobileOpen} to="/education-reports" icon={BarChart3} onClick={handleNavClick}>Usage Reports</NavItem>}
          </>}

          {/* Tools */}
          {adv && canAccessPage("calculator") && <NavGroup label="Tools" collapsed={collapsed && !mobileOpen} open={sections.tools} onToggle={() => toggle("tools")} />}
          {adv && ((collapsed && !mobileOpen) || sections.tools) && <>
            {canAccessPage('calculator') && <NavItem collapsed={collapsed && !mobileOpen} to="/calculator" icon={Calculator} onClick={handleNavClick}>Calculator</NavItem>}
          </>}

          {/* Settings */}
          <div className="mt-2" style={{ borderTop: '1px solid var(--brand-sidebar-border)', paddingTop: '0.5rem' }}>
            {canAccessPage('settings') && <NavItem collapsed={collapsed && !mobileOpen} to="/settings" icon={Settings} onClick={handleNavClick}>Settings</NavItem>}
          </div>
        </nav>

        {/* Fleet Status */}
        {printersData && (!collapsed || mobileOpen) && (
          <NavLink to="/printers" className="flex-shrink-0 px-4 py-3 hover:opacity-80 transition-opacity overflow-hidden" style={{ borderTop: '1px solid var(--brand-sidebar-border)' }}>
            <div className="flex items-center gap-2 mb-1.5">
              <span className="text-xs font-medium whitespace-nowrap" style={{ color: 'var(--brand-sidebar-text)' }}>
                {printersData.filter(p => p.last_seen && (Date.now() - new Date(p.last_seen + 'Z').getTime()) < 90000).length}/{printersData.length} online
              </span>
            </div>
            <div className="flex flex-wrap gap-0.5">
              {printersData.map(p => {
                const online = p.last_seen && (Date.now() - new Date(p.last_seen + 'Z').getTime()) < 90000
                return <div key={p.id} className={`w-2 h-2 rounded-full ${online ? 'bg-green-500' : 'bg-farm-600'}`} />
              })}
            </div>
          </NavLink>
        )}

        {/* Footer */}
        <div className="flex-shrink-0 px-4 py-3" style={{ borderTop: '1px solid var(--brand-sidebar-border)' }}>
          <button
            onClick={() => {
              localStorage.removeItem("token");
              localStorage.removeItem("user");
              window.location.href = "/login";
            }}
            className="flex items-center gap-2 hover:text-red-400 text-sm mt-2 transition-colors"
            style={{ color: 'var(--brand-sidebar-text)' }}
          >
            <LogOut size={14} />
            {(!collapsed || mobileOpen) && "Logout"}
          </button>
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
      title={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
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
        >
          <Menu size={24} />
        </button>
      </div>
    </header>
  )
}


function isTokenValid() {
  const token = localStorage.getItem('token')
  if (!token) return false
  try {
    const payload = JSON.parse(atob(token.split('.')[1]))
    return payload.exp * 1000 > Date.now()
  } catch {
    return false
  }
}


function ProtectedRoute({ children }) {
  const location = useLocation()
  if (!isTokenValid()) {
    localStorage.removeItem('token')
    localStorage.removeItem('user')
    return <Navigate to="/login" state={{ from: location }} replace />
  }
  return children
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
    if (location.pathname === '/setup' || location.pathname === '/login') return
    const API_KEY = import.meta.env.VITE_API_KEY
    const headers = { 'Content-Type': 'application/json' }
    if (API_KEY) headers['X-API-Key'] = API_KEY
    fetch('/api/setup/status', { headers })
      .then(r => r.json())
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

  return (
    <ProtectedRoute>
      <div className="h-screen flex flex-col md:flex-row overflow-hidden">
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
          <div className="hidden md:flex items-center justify-end gap-3 p-4 border-b border-farm-800" style={{ backgroundColor: 'var(--brand-content-bg)' }}>
            <GlobalSearch />
            <ThemeToggle />
            <AlertBell />
          </div>
          <main className="flex-1 overflow-auto" style={{ backgroundColor: 'var(--brand-content-bg)' }}>
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/timeline" element={<Timeline />} />
            <Route path="/jobs" element={<Jobs />} />
            <Route path="/printers" element={<Printers />} />
            <Route path="/models" element={<Models />} />
            <Route path="/calculator" element={<CalculatorPage />} />
            <Route path="/analytics" element={<ProGate feature="analytics"><Analytics /></ProGate>} />
            <Route path="/utilization" element={<ProGate feature="analytics"><Utilization /></ProGate>} />
            <Route path="/upload" element={<Upload />} />
            <Route path="/spools" element={<Spools />} />
            <Route path="/consumables" element={<ProGate feature="products"><Consumables /></ProGate>} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/admin" element={<Navigate to="/settings" replace />} />
            <Route path="/permissions" element={<ProGate feature="permissions"><Navigate to="/settings" replace /></ProGate>} />
            <Route path="/maintenance" element={<ProGate feature="maintenance"><Maintenance /></ProGate>} />
            <Route path="/cameras" element={<Cameras />} />
            <Route path="/branding" element={<ProGate feature="branding"><Navigate to="/settings" replace /></ProGate>} />
            <Route path="/products" element={<ProGate feature="products"><Products /></ProGate>} />
            <Route path="/orders" element={<ProGate feature="orders"><Orders /></ProGate>} />
            <Route path="/alerts" element={<Alerts />} />
            <Route path="/detections" element={<Detections />} />
            <Route path="/education-reports" element={<ProGate feature="usage_reports" tier="Education"><EducationReports /></ProGate>} />
          </Routes>
      {showHelp && <KeyboardShortcutsModal onClose={() => setShowHelp(false)} />}
            <EmergencyStop />
          </main>
        </div>
      </div>
    </ProtectedRoute>
  )
}
