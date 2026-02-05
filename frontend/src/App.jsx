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
  Wrench,
  ShoppingCart,
} from 'lucide-react'
import clsx from 'clsx'
import { useBranding } from './BrandingContext'
import GlobalSearch from './components/GlobalSearch'
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
import Products from './pages/Products'
import Orders from './pages/Orders'
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


function Sidebar({ mobileOpen, onMobileClose }) {
  const [collapsed, setCollapsed] = useState(false)
  const [sections, setSections] = useState({ work: true, library: true, analyze: true, system: true })
  const toggle = (key) => setSections(s => ({ ...s, [key]: !s[key] }))
  const branding = useBranding()

  const { data: statsData } = useQuery({
    queryKey: ['stats'],
    queryFn: stats.get,
    refetchInterval: 30000,
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

          {/* Work */}
          {(canAccessPage("jobs") || canAccessPage("upload") || canAccessPage("maintenance")) && <NavGroup label="Work" collapsed={collapsed && !mobileOpen} open={sections.work} onToggle={() => toggle("work")} />}
          {((collapsed && !mobileOpen) || sections.work) && <>
            {canAccessPage('jobs') && <NavItem collapsed={collapsed && !mobileOpen} to="/jobs" icon={ListTodo} onClick={handleNavClick}>Jobs</NavItem>}
            {canAccessPage('timeline') && <NavItem collapsed={collapsed && !mobileOpen} to="/timeline" icon={Calendar} onClick={handleNavClick}>Timeline</NavItem>}
            {canAccessPage('upload') && <NavItem collapsed={collapsed && !mobileOpen} to="/upload" icon={UploadIcon} onClick={handleNavClick}>Upload</NavItem>}
            {canAccessPage('maintenance') && <NavItem collapsed={collapsed && !mobileOpen} to="/maintenance" icon={Wrench} onClick={handleNavClick}>Maintenance</NavItem>}
            {canAccessPage('jobs') && <NavItem collapsed={collapsed && !mobileOpen} to="/orders" icon={ShoppingCart} onClick={handleNavClick}>Orders</NavItem>}
          </>}

          {/* Library */}
          {(canAccessPage("models") || canAccessPage("spools")) && <NavGroup label="Library" collapsed={collapsed && !mobileOpen} open={sections.library} onToggle={() => toggle("library")} />}
          {((collapsed && !mobileOpen) || sections.library) && <>
            {canAccessPage('models') && <NavItem collapsed={collapsed && !mobileOpen} to="/products" icon={Package} onClick={handleNavClick}>Products</NavItem>}
            {canAccessPage('models') && <NavItem collapsed={collapsed && !mobileOpen} to="/models" icon={Package} onClick={handleNavClick}>Models</NavItem>}
            {canAccessPage('spools') && <NavItem collapsed={collapsed && !mobileOpen} to="/spools" icon={Package} onClick={handleNavClick}>Spools</NavItem>}
          </>}

          {/* Analyze */}
          {(canAccessPage("analytics") || canAccessPage("calculator")) && <NavGroup label="Analyze" collapsed={collapsed && !mobileOpen} open={sections.analyze} onToggle={() => toggle("analyze")} />}
          {((collapsed && !mobileOpen) || sections.analyze) && <>
            {canAccessPage('analytics') && <NavItem collapsed={collapsed && !mobileOpen} to="/analytics" icon={BarChart3} onClick={handleNavClick}>Analytics</NavItem>}
            {canAccessPage('calculator') && <NavItem collapsed={collapsed && !mobileOpen} to="/calculator" icon={Calculator} onClick={handleNavClick}>Calculator</NavItem>}
          </>}

          {/* System */}
          {(canAccessPage("settings") || canAccessPage("admin")) && <NavGroup label="System" collapsed={collapsed && !mobileOpen} open={sections.system} onToggle={() => toggle("system")} />}
          {((collapsed && !mobileOpen) || sections.system) && <>
            {canAccessPage('settings') && <NavItem collapsed={collapsed && !mobileOpen} to="/settings" icon={Settings} onClick={handleNavClick}>Settings</NavItem>}
            {canAccessPage('admin') && <NavItem collapsed={collapsed && !mobileOpen} to="/admin" icon={Shield} onClick={handleNavClick}>Admin</NavItem>}
            {canAccessPage('admin') && <NavItem collapsed={collapsed && !mobileOpen} to="/permissions" icon={Shield} onClick={handleNavClick}>Permissions</NavItem>}
            {canAccessPage('admin') && <NavItem collapsed={collapsed && !mobileOpen} to="/branding" icon={Palette} onClick={handleNavClick}>Branding</NavItem>}
          </>}
        </nav>

        {/* Quick Stats */}
        {statsData && (!collapsed || mobileOpen) && (
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
            {(!collapsed || mobileOpen) && <span>{branding.footer_text}</span>}
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
            {(!collapsed || mobileOpen) && "Logout"}
          </button>
        </div>
      </aside>
    </>
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
  const location = useLocation()
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false)

  // Close mobile menu on route change
  useEffect(() => {
    setMobileMenuOpen(false)
  }, [location.pathname])

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
          <div className="hidden md:flex items-center justify-end p-4 border-b border-farm-800" style={{ backgroundColor: 'var(--brand-content-bg)' }}>
            <GlobalSearch />
          </div>
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
            <Route path="/products" element={<Products />} />
            <Route path="/orders" element={<Orders />} />
          </Routes>
          </main>
        </div>
      </div>
    </ProtectedRoute>
  )
}
