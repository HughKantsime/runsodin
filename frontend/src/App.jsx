import { useState, useEffect } from "react"
import { canAccessPage, canDo, getCurrentUser } from './permissions'
import { Routes, Route, NavLink, Navigate, useLocation } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { 
  LayoutDashboard,
  Video, 
  Calendar, 
  Printer, 
  Package, 
  ListTodo,
  Settings,
  Activity,
  BarChart3,
  Calculator,
  Upload as UploadIcon,
  Shield,
  LogOut,
  ChevronLeft,
  ChevronRight,
  Menu,
  X,
  Palette,
  ChevronDown,
 Wrench, } from 'lucide-react'
import clsx from 'clsx'
import { useBranding } from './BrandingContext'
import Dashboard from './pages/Dashboard'
import Timeline from './pages/Timeline'
import Jobs from './pages/Jobs'
import Printers from './pages/Printers'
import Models from './pages/Models'
import CalculatorPage from './pages/Calculator'
import Analytics from './pages/Analytics'
import SettingsPage from './pages/Settings'
import Spools from './pages/Spools'
import Upload from './pages/Upload'
import Login from './pages/Login'
import Admin from './pages/Admin'
import Maintenance from './pages/Maintenance'
import Permissions from './pages/Permissions'
import Cameras from "./pages/Cameras"
import Branding from './pages/Branding'
import { stats } from './api'


function NavItem({ to, icon: Icon, children, collapsed, onClick }) {
  return (
    <NavLink 
      to={to}
      onClick={onClick}
      className={({ isActive }) => clsx(
        collapsed ? 'flex items-center justify-center py-2.5 rounded-lg transition-colors' 
                  : 'flex items-center gap-3 px-4 py-2.5 rounded-lg transition-colors',
      )}
      style={({ isActive }) => isActive
        ? { backgroundColor: 'var(--brand-sidebar-active-bg)', color: 'var(--brand-sidebar-active-text)' }
        : { color: 'var(--brand-sidebar-text)' }
      }
    >
      <Icon size={20} className="flex-shrink-0" />
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
          <span className="text-[10px] uppercase tracking-widest font-semibold"
            style={{ color: 'var(--brand-text-muted)' }}>
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


function Sidebar() {
  const [collapsed, setCollapsed] = useState(false)
  const [sections, setSections] = useState({ work: true, library: true, analyze: true, system: true })
  const toggle = (key) => setSections(s => ({ ...s, [key]: !s[key] }))
  const branding = useBranding()

  const { data: statsData } = useQuery({
    queryKey: ['stats'],
    queryFn: stats.get,
    refetchInterval: 30000,
  })

  return (
    <aside 
      className={clsx("flex flex-col h-full transition-all duration-300", collapsed ? "w-16" : "w-64")}
      style={{ 
        backgroundColor: 'var(--brand-sidebar-bg)',
        borderRight: '1px solid var(--brand-sidebar-border)',
      }}
    >
      {/* Logo */}
      <div 
        className={clsx("flex items-center", collapsed ? "p-3 justify-center" : "p-6 justify-between")}
        style={{ borderBottom: '1px solid var(--brand-sidebar-border)' }}
      >
        <div>
          {!collapsed && (
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
        <button 
          onClick={() => setCollapsed(!collapsed)} 
          className="transition-colors"
          style={{ color: 'var(--brand-sidebar-text)' }}
        >
          {collapsed ? <ChevronRight size={20} /> : <ChevronLeft size={20} />}
        </button>
      </div>

      {/* Navigation */}
      <nav className="flex-1 overflow-y-auto p-4 space-y-1 min-h-0">
        {/* Monitor */}
        {canAccessPage('dashboard') && <NavItem collapsed={collapsed} to="/" icon={LayoutDashboard}>Dashboard</NavItem>}
        {canAccessPage('printers') && <NavItem collapsed={collapsed} to="/printers" icon={Printer}>Printers</NavItem>}
        {canAccessPage('cameras') && <NavItem collapsed={collapsed} to="/cameras" icon={Video}>Cameras</NavItem>}

        {/* Work */}
        {(canAccessPage("jobs") || canAccessPage("upload") || canAccessPage("maintenance")) && <NavGroup label="Work" collapsed={collapsed} open={sections.work} onToggle={() => toggle("work")} />}
        {(collapsed || sections.work) && <>
          {canAccessPage('jobs') && <NavItem collapsed={collapsed} to="/jobs" icon={ListTodo}>Jobs</NavItem>}
          {canAccessPage('timeline') && <NavItem collapsed={collapsed} to="/timeline" icon={Calendar}>Timeline</NavItem>}
          {canAccessPage('upload') && <NavItem collapsed={collapsed} to="/upload" icon={UploadIcon}>Upload</NavItem>}
          {canAccessPage('maintenance') && <NavItem collapsed={collapsed} to="/maintenance" icon={Wrench}>Maintenance</NavItem>}
        </>}

        {/* Library */}
        {(canAccessPage("models") || canAccessPage("spools")) && <NavGroup label="Library" collapsed={collapsed} open={sections.library} onToggle={() => toggle("library")} />}
        {(collapsed || sections.library) && <>
          {canAccessPage('models') && <NavItem collapsed={collapsed} to="/models" icon={Package}>Models</NavItem>}
          {canAccessPage('spools') && <NavItem collapsed={collapsed} to="/spools" icon={Package}>Spools</NavItem>}
        </>}

        {/* Analyze */}
        {(canAccessPage("analytics") || canAccessPage("calculator")) && <NavGroup label="Analyze" collapsed={collapsed} open={sections.analyze} onToggle={() => toggle("analyze")} />}
        {(collapsed || sections.analyze) && <>
          {canAccessPage('analytics') && <NavItem collapsed={collapsed} to="/analytics" icon={BarChart3}>Analytics</NavItem>}
          {canAccessPage('calculator') && <NavItem collapsed={collapsed} to="/calculator" icon={Calculator}>Calculator</NavItem>}
        </>}

        {/* System */}
        {(canAccessPage("settings") || canAccessPage("admin")) && <NavGroup label="System" collapsed={collapsed} open={sections.system} onToggle={() => toggle("system")} />}
        {(collapsed || sections.system) && <>
          {canAccessPage('settings') && <NavItem collapsed={collapsed} to="/settings" icon={Settings}>Settings</NavItem>}
          {canAccessPage('admin') && <NavItem collapsed={collapsed} to="/admin" icon={Shield}>Admin</NavItem>}
          {canAccessPage('admin') && <NavItem collapsed={collapsed} to="/permissions" icon={Shield}>Permissions</NavItem>}
          {canAccessPage('admin') && <NavItem collapsed={collapsed} to="/branding" icon={Palette}>Branding</NavItem>}
        </>}
      </nav>

      {/* Quick Stats */}
      {statsData && !collapsed && (
        <div className="flex-shrink-0 p-4" style={{ borderTop: '1px solid var(--brand-sidebar-border)' }}>
          <div className="grid grid-cols-2 gap-3">
            <div className="rounded-lg p-3" style={{ backgroundColor: 'var(--brand-card-bg)' }}>
              <div className="text-2xl font-bold" style={{ color: 'var(--brand-accent)' }}>
                {statsData.jobs?.printing || 0}
              </div>
              <div className="text-xs" style={{ color: 'var(--brand-text-muted)' }}>Printing</div>
            </div>
            <div className="rounded-lg p-3" style={{ backgroundColor: 'var(--brand-card-bg)' }}>
              <div className="text-2xl font-bold text-status-pending">
                {statsData.jobs?.pending || 0}
              </div>
              <div className="text-xs" style={{ color: 'var(--brand-text-muted)' }}>Pending</div>
            </div>
          </div>
        </div>
      )}

      {/* Footer */}
      <div className="flex-shrink-0 p-4" style={{ borderTop: '1px solid var(--brand-sidebar-border)' }}>
        <div className="flex items-center gap-2 text-sm" style={{ color: 'var(--brand-sidebar-text)' }}>
          <Activity size={14} className="text-green-500" />
          {!collapsed && <span>{branding.footer_text}</span>}
        </div>
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
          {!collapsed && "Logout"}
        </button>
      </div>
    </aside>
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
  const location = useLocation()

  // Show login page without sidebar
  if (location.pathname === '/login') {
    return <Login />
  }

  return (
    <ProtectedRoute>
      <div className="h-screen flex overflow-hidden">
        <Sidebar />
        <main className="flex-1 overflow-auto" style={{ backgroundColor: 'var(--brand-content-bg)' }}>
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/timeline" element={<Timeline />} />
            <Route path="/jobs" element={<Jobs />} />
            <Route path="/printers" element={<Printers />} />
            <Route path="/models" element={<Models />} />
            <Route path="/calculator" element={<CalculatorPage />} />
            <Route path="/analytics" element={<Analytics />} />
            <Route path="/upload" element={<Upload />} />
            <Route path="/spools" element={<Spools />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/admin" element={<Admin />} />
            <Route path="/permissions" element={<Permissions />} />
              <Route path="/maintenance" element={<Maintenance />} />
              <Route path="/cameras" element={<Cameras />} />
            <Route path="/branding" element={<Branding />} />
          </Routes>
        </main>
      </div>
    </ProtectedRoute>
  )
}
