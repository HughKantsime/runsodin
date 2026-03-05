import { useState } from "react"
import { NavLink } from 'react-router-dom'
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
  X,
  ChevronDown,
  Wrench,
  ShoppingCart,
  Bell as BellIcon,
  ShoppingBag,
  Circle,
  Package,
  Eye,
  FileText,
  Archive,
  SlidersHorizontal,
} from 'lucide-react'
import clsx from 'clsx'
import { useBranding } from '../../BrandingContext'
import { useLicense } from '../../LicenseContext'
import { useOrg } from '../../contexts/OrgContext'
import ProBadge from '../shared/ProBadge'
import { canAccessPage } from '../../permissions'
import { isOnline } from '../../utils/shared'
import { stats, printers, getEducationMode, pricingConfig } from '../../api'
import { Modal, Button } from '../ui'


export function NavItem({ to, icon: Icon, children, collapsed, onClick }) {
  // Extract plain text from children for the tooltip (strip ProBadge etc.)
  const label = typeof children === 'string' ? children : Array.isArray(children) ? children.find(c => typeof c === 'string') : undefined
  return (
    <NavLink
      to={to}
      onClick={onClick}
      title={collapsed ? (label || undefined) : undefined}
      className={({ isActive }) => clsx(
        'transition-colors border-l-2',
        collapsed ? 'flex items-center justify-center py-1.5 rounded-sm'
                  : 'flex items-center gap-2.5 px-3 py-1.5 rounded-sm text-sm',
        isActive ? 'border-l-[var(--brand-primary)]' : 'border-l-transparent',
      )}
      style={({ isActive }) => isActive
        ? { backgroundColor: 'var(--brand-sidebar-active-bg)', color: 'var(--brand-sidebar-active-text)' }
        : { color: 'var(--brand-sidebar-text)' }
      }
    >
      <Icon size={16} className="flex-shrink-0" />
      {!collapsed && <span className="font-medium">{children}</span>}
    </NavLink>
  )
}


export function NavGroup({ label, collapsed, open, onToggle }) {
  return (
    <div className="pt-3 pb-1">
      <div style={{ borderTop: '1px solid var(--brand-sidebar-border)' }} />
      {!collapsed && (
        <button
          onClick={onToggle}
          className="flex items-center justify-between w-full px-3 mt-1.5 group"
        >
          <span className="text-[10px] uppercase font-mono font-medium"
            style={{ color: 'var(--brand-text-muted)', letterSpacing: '0.2em' }}>
            {label}
          </span>
          <ChevronDown
            size={10}
            className={clsx("transition-transform duration-200", open ? "" : "-rotate-90")}
            style={{ color: 'var(--brand-text-muted)' }}
          />
        </button>
      )}
    </div>
  )
}


export default function Sidebar({ mobileOpen, onMobileClose }) {
  const [collapsed, setCollapsed] = useState(false)
  const [sections, setSections] = useState({ work: true, library: true, insights: true, tools: true })
  const [showLogoutConfirm, setShowLogoutConfirm] = useState(false)

  // React Query for ui mode (replaces window event listener)
  const { data: pricingData } = useQuery({
    queryKey: ['pricing-config'],
    queryFn: () => pricingConfig.get(),
    refetchInterval: 30000,
  })
  const uiMode = pricingData?.ui_mode || 'advanced'

  // React Query for education mode (replaces window event listener)
  const { data: educationData } = useQuery({
    queryKey: ['education-mode'],
    queryFn: () => getEducationMode(),
    refetchInterval: 30000,
  })
  const educationMode = educationData?.enabled || false

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
          className={clsx("flex items-center", collapsed && !mobileOpen ? "p-2.5 justify-center" : "p-4 justify-between")}
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
              className="w-full bg-[var(--brand-surface)] border border-[var(--brand-card-border)] rounded-lg px-2 py-1.5 text-xs"
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
          {/* Fleet */}
          {canAccessPage('dashboard') && <NavItem collapsed={collapsed && !mobileOpen} to="/" icon={LayoutDashboard} onClick={handleNavClick}>Dashboard</NavItem>}
          {canAccessPage('printers') && <NavItem collapsed={collapsed && !mobileOpen} to="/printers" icon={Printer} onClick={handleNavClick}>Printers</NavItem>}
          {canAccessPage('cameras') && <NavItem collapsed={collapsed && !mobileOpen} to="/cameras" icon={Video} onClick={handleNavClick}>Cameras</NavItem>}
          <NavItem collapsed={collapsed && !mobileOpen} to="/archives" icon={Archive} onClick={handleNavClick}>Archives</NavItem>
          <NavItem collapsed={collapsed && !mobileOpen} to="/alerts" icon={BellIcon} onClick={handleNavClick}>Alerts</NavItem>
          <NavItem collapsed={collapsed && !mobileOpen} to="/detections" icon={Eye} onClick={handleNavClick}>Detections</NavItem>

          {/* Work */}
          {(canAccessPage("jobs") || canAccessPage("upload") || canAccessPage("timeline")) && <NavGroup label="Work" collapsed={collapsed && !mobileOpen} open={sections.work} onToggle={() => toggle("work")} />}
          {((collapsed && !mobileOpen) || sections.work) && <>
            {canAccessPage('jobs') && <NavItem collapsed={collapsed && !mobileOpen} to="/jobs" icon={ListTodo} onClick={handleNavClick}>Jobs</NavItem>}
            {canAccessPage('timeline') && <NavItem collapsed={collapsed && !mobileOpen} to="/timeline" icon={Calendar} onClick={handleNavClick}>Timeline</NavItem>}
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

          {/* Insights (was Monitor — Pro only) */}
          {adv && lic.isPro && (canAccessPage("analytics") || canAccessPage("maintenance")) && <NavGroup label="Insights" collapsed={collapsed && !mobileOpen} open={sections.insights} onToggle={() => toggle("insights")} />}
          {adv && lic.isPro && ((collapsed && !mobileOpen) || sections.insights) && <>
            {canAccessPage('maintenance') && <NavItem collapsed={collapsed && !mobileOpen} to="/maintenance" icon={Wrench} onClick={handleNavClick}>Maintenance</NavItem>}
            {canAccessPage('analytics') && <NavItem collapsed={collapsed && !mobileOpen} to="/analytics" icon={BarChart3} onClick={handleNavClick}>Analytics</NavItem>}
            {canAccessPage('analytics') && <NavItem collapsed={collapsed && !mobileOpen} to="/utilization" icon={Activity} onClick={handleNavClick}>Utilization</NavItem>}
            {canAccessPage('education_reports') && <NavItem collapsed={collapsed && !mobileOpen} to="/education-reports" icon={BarChart3} onClick={handleNavClick}>Usage Reports</NavItem>}
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
          <NavLink to="/printers" className="flex-shrink-0 px-3 py-2.5 hover:opacity-80 transition-opacity overflow-hidden" style={{ borderTop: '1px solid var(--brand-sidebar-border)' }} aria-label={`Fleet status: ${printersData.filter(p => isOnline(p)).length} of ${printersData.length} printers online`}>
            <div className="flex items-center justify-between mb-1.5">
              <span className="text-[10px] font-mono font-medium" style={{ color: 'var(--brand-text-muted)' }}>
                FLEET
              </span>
              <span className="text-[10px] font-mono" style={{ color: 'var(--brand-sidebar-text)' }}>
                {printersData.filter(p => isOnline(p)).length}/{printersData.length}
              </span>
            </div>
            <div className="h-1 rounded-full overflow-hidden" style={{ backgroundColor: 'rgb(var(--farm-800))' }}>
              <div
                className="h-full rounded-full transition-all duration-500"
                style={{
                  width: `${printersData.length ? (printersData.filter(p => isOnline(p)).length / printersData.length * 100) : 0}%`,
                  backgroundColor: 'var(--status-completed)'
                }}
              />
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
              onClick={() => setShowLogoutConfirm(true)}
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

      <Modal isOpen={showLogoutConfirm} onClose={() => setShowLogoutConfirm(false)} title="Confirm Logout" size="sm" alert>
        <p className="text-sm mb-4" style={{ color: 'var(--brand-text-secondary)' }}>Are you sure you want to log out?</p>
        <div className="flex justify-end gap-2">
          <Button variant="secondary" size="sm" onClick={() => setShowLogoutConfirm(false)}>Cancel</Button>
          <Button variant="danger" size="sm" onClick={async () => { await fetch('/api/auth/logout', { method: 'POST', credentials: 'include' }).catch(() => {}); window.location.href = '/login'; }}>Logout</Button>
        </div>
      </Modal>
    </>
  )
}
