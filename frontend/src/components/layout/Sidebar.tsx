import { useTranslation } from '../../contexts/I18nContext'
import { useState } from "react"
import { NavLink } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  LayoutDashboard,
  Video,
  Film,
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
  GraduationCap,
  FolderKanban,
  ScrollText,
} from 'lucide-react'
import clsx from 'clsx'
import { useBranding } from '../../BrandingContext'
import { useLicense } from '../../LicenseContext'
import { useOrg } from '../../contexts/OrgContext'
import ProBadge from '../shared/ProBadge'
import { canAccessPage, clearSensitiveBrowserState } from '../../permissions'
import { isOnline, isPrintingState } from '../../utils/shared'
import { stats, printers, getEducationMode, pricingConfig, alerts } from '../../api'
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
      <Icon size={16} className="shrink-0" />
      {!collapsed && <span className="font-medium">{children}</span>}
    </NavLink>
  )
}


export function NavGroup({ label, collapsed, open, onToggle }) {
  return (
    <div className="sidebar-nav-group pt-3 pb-1">
      <div style={{ borderTop: '1px solid var(--brand-sidebar-border)' }} />
      {!collapsed && (
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={open}
          className="flex items-center justify-between w-full min-h-11 md:min-h-6 px-3 mt-1.5 group"
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


const SECTIONS_KEY = 'odin.sidebar.sections'
const DEFAULT_SECTIONS = { print: true, farm: true, materials: true, history: false, insights: false }

function loadSections() {
  try {
    const saved = JSON.parse(localStorage.getItem(SECTIONS_KEY) || 'null')
    if (saved && typeof saved === 'object') return { ...DEFAULT_SECTIONS, ...Object.fromEntries(Object.entries(saved).filter(([k, v]) => k in DEFAULT_SECTIONS && typeof v === 'boolean')) }
  } catch { /* unreadable: use the defaults */ }
  return { ...DEFAULT_SECTIONS }
}

/** A count at the right edge of a row. It sits beside the link, not inside it, so the link keeps
 *  its plain name ("Jobs"); the count has its own label for screen readers. Collapsed: a dot. */
export function NavCount({ count, label, tone = 'muted', collapsed, text }: { count: number; label: string; tone?: 'muted' | 'hot'; collapsed?: boolean; text?: string }) {
  if (!count) return null
  const hot = tone === 'hot'
  if (collapsed) return <span role="status" aria-label={label} title={label} className="pointer-events-none absolute right-2 top-1 h-1.5 w-1.5 rounded-full" style={{ backgroundColor: hot ? 'var(--status-failed, #dc2626)' : 'var(--brand-text-muted)' }} />
  return <span role="status" aria-label={label} title={label}
    className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 rounded-full px-1.5 text-[10px] font-mono leading-4"
    style={hot ? { backgroundColor: 'var(--status-failed, #dc2626)', color: '#fff' } : { backgroundColor: 'var(--brand-sidebar-active-bg)', color: 'var(--brand-sidebar-text)' }}>
    {text || (count > 99 ? '99+' : count)}
  </span>
}

export default function Sidebar({ mobileOpen, onMobileClose }) {
  const { t } = useTranslation()
  const [collapsed, setCollapsed] = useState(false)
  // Which groups are open. History and Insights start folded; each person's choice is remembered.
  const [sections, setSections] = useState(loadSections)
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
  const toggle = (key) => setSections(s => {
    const next = { ...s, [key]: !s[key] }
    try { localStorage.setItem(SECTIONS_KEY, JSON.stringify(next)) } catch { /* private mode: not remembered */ }
    return next
  })
  const branding = useBranding()

  const { data: statsData } = useQuery({
    queryKey: ['stats'],
    queryFn: () => stats.get(),
    refetchInterval: 30000,
  })
  const { data: printersData } = useQuery({
    queryKey: ['sidebar-printers', org.orgId],
    queryFn: () => printers.list(false, '', org.orgId),
    refetchInterval: 15000,
  })

  const { data: unreadData } = useQuery({
    queryKey: ['sidebar-unread-alerts'],
    queryFn: () => alerts.unreadCount(),
    refetchInterval: 30000,
  })
  const unreadAlerts = Number(unreadData?.unread_count) || 0
  const waitingJobs = (Number(statsData?.jobs?.pending) || 0) + (Number(statsData?.jobs?.scheduled) || 0)
  const { data: principal } = useQuery<{ username?: string; role?: string } | null>({ queryKey: ['auth', 'me'], enabled: false })

  const onlinePrinters = (printersData || []).filter(isOnline)
  const printingCount = onlinePrinters.filter(p => isPrintingState(p.gcode_state)).length
  const idleCount = onlinePrinters.filter(p => ['IDLE', 'READY', 'FINISH', 'COMPLETED'].includes(p.gcode_state?.trim().toUpperCase())).length
  const otherCount = onlinePrinters.length - printingCount - idleCount
  const offlineCount = (printersData?.length || 0) - onlinePrinters.length

  const c = collapsed && !mobileOpen   // icon-only rail (never on the phone drawer)

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
          aria-hidden="true"
          onClick={onMobileClose}
        />
      )}

      <aside
        role="navigation"
        aria-label="Main navigation"
        className={clsx(
          "sidebar flex flex-col h-full transition-all duration-300",
          // Mobile: fixed drawer, hidden by default
          "fixed md:relative z-50 md:z-auto",
          mobileOpen ? "translate-x-0" : "-translate-x-full md:translate-x-0",
          // Width
          collapsed ? "w-64 md:w-16" : "w-64"
        )}
        style={{
          backgroundColor: 'var(--brand-sidebar-bg)',
          borderRight: '1px solid var(--brand-sidebar-border)',
          touchAction: 'manipulation',
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
            <select aria-label="Organization"
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
        <nav className="sidebar-nav flex-1 overflow-y-auto p-4 space-y-1 min-h-0">
          {canAccessPage('dashboard') && <NavItem collapsed={c} to="/" icon={LayoutDashboard} onClick={handleNavClick}>{t('nav.dashboard')}</NavItem>}

          {/* Print: getting something printed */}
          {(canAccessPage('jobs') || canAccessPage('upload') || canAccessPage('timeline') || canAccessPage('models')) && <NavGroup label={t('nav.print')} collapsed={c} open={sections.print} onToggle={() => toggle('print')} />}
          {(c || sections.print) && <>
            {educationMode && lic.isEducation && canAccessPage('education') && <NavItem collapsed={c} to="/education" icon={GraduationCap} onClick={handleNavClick}>Education</NavItem>}
            {canAccessPage('jobs') && <div className="relative"><NavItem collapsed={c} to="/jobs" icon={ListTodo} onClick={handleNavClick}>{t('nav.jobs')}</NavItem><NavCount collapsed={c} count={waitingJobs} text={`${waitingJobs > 99 ? '99+' : waitingJobs} waiting`} label={`${waitingJobs} ${waitingJobs === 1 ? 'job' : 'jobs'} waiting`} /></div>}
            {canAccessPage('timeline') && <NavItem collapsed={c} to="/timeline" icon={Calendar} onClick={handleNavClick}>{t('nav.timeline')}</NavItem>}
            {canAccessPage('upload') && <NavItem collapsed={c} to="/upload" icon={UploadIcon} onClick={handleNavClick}>{t('nav.upload')}</NavItem>}
            {canAccessPage('models') && <NavItem collapsed={c} to="/models" icon={Box} onClick={handleNavClick}>{t('nav.models')}</NavItem>}
            {adv && lic.isPro && !educationMode && canAccessPage('jobs') && <NavItem collapsed={c} to="/orders" icon={ShoppingCart} onClick={handleNavClick}>{t('nav.orders')}{!lic.isPro && <ProBadge />}</NavItem>}
          </>}

          {/* Farm: the machines */}
          <NavGroup label={t('nav.farm')} collapsed={c} open={sections.farm} onToggle={() => toggle('farm')} />
          {(c || sections.farm) && <>
            {canAccessPage('printers') && <div className="relative">
              <NavItem collapsed={c} to="/printers" icon={Printer} onClick={handleNavClick}>{t('nav.printers')}</NavItem>
              {printersData && !c && (
                <NavLink to="/printers" onClick={handleNavClick}
                  aria-label={`Fleet status: ${onlinePrinters.length} of ${printersData.length} printers online`}
                  title={`Printing: ${printingCount} · Idle: ${idleCount} · Offline: ${offlineCount}${otherCount > 0 ? ` · Other: ${otherCount}` : ''}`}
                  className="absolute right-2 top-1/2 -translate-y-1/2 rounded-full px-1.5 text-[10px] font-mono leading-4 hover:opacity-80"
                  style={{ backgroundColor: 'var(--brand-sidebar-active-bg)', color: offlineCount > 0 ? 'var(--brand-sidebar-text)' : 'var(--status-completed)' }}>
                  {onlinePrinters.length}/{printersData.length}
                  <span className="sr-only"> Printing: {printingCount} Idle: {idleCount} Offline: {offlineCount}{otherCount > 0 ? ` Other: ${otherCount}` : ''}</span>
                </NavLink>
              )}
            </div>}
            {canAccessPage('cameras') && <NavItem collapsed={c} to="/cameras" icon={Video} onClick={handleNavClick}>{t('nav.cameras')}</NavItem>}
            <div className="relative"><NavItem collapsed={c} to="/alerts" icon={BellIcon} onClick={handleNavClick}>{t('nav.alerts')}</NavItem><NavCount collapsed={c} tone="hot" count={unreadAlerts} label={`${unreadAlerts} unread ${unreadAlerts === 1 ? 'alert' : 'alerts'}`} /></div>
            {adv && lic.isPro && canAccessPage('maintenance') && <NavItem collapsed={c} to="/maintenance" icon={Wrench} onClick={handleNavClick}>{t('nav.maintenance')}</NavItem>}
          </>}

          {/* Materials: what goes into a print */}
          {(canAccessPage('spools') || canAccessPage('models')) && <NavGroup label={t('nav.materials')} collapsed={c} open={sections.materials} onToggle={() => toggle('materials')} />}
          {(c || sections.materials) && <>
            {canAccessPage('spools') && <NavItem collapsed={c} to="/spools" icon={Circle} onClick={handleNavClick}>{t('nav.spools')}</NavItem>}
            <NavItem collapsed={c} to="/profiles" icon={SlidersHorizontal} onClick={handleNavClick}>Profiles</NavItem>
            {adv && lic.isPro && !educationMode && canAccessPage('models') && <NavItem collapsed={c} to="/products" icon={ShoppingBag} onClick={handleNavClick}>{t('nav.products')}{!lic.isPro && <ProBadge />}</NavItem>}
            {adv && lic.isPro && !educationMode && canAccessPage('models') && <NavItem collapsed={c} to="/consumables" icon={Package} onClick={handleNavClick}>Consumables{!lic.isPro && <ProBadge />}</NavItem>}
          </>}

          {/* History: what already happened. Folded until opened. */}
          <NavGroup label={t('nav.history')} collapsed={c} open={sections.history} onToggle={() => toggle('history')} />
          {(c || sections.history) && <>
            <NavItem collapsed={c} to="/archives" icon={Archive} onClick={handleNavClick}>Archives</NavItem>
            <NavItem collapsed={c} to="/print-log" icon={ScrollText} onClick={handleNavClick}>Print Log</NavItem>
            {canAccessPage('cameras') && <NavItem collapsed={c} to="/timelapses" icon={Film} onClick={handleNavClick}>Timelapses</NavItem>}
            <NavItem collapsed={c} to="/projects" icon={FolderKanban} onClick={handleNavClick}>Projects</NavItem>
            <NavItem collapsed={c} to="/detections" icon={Eye} onClick={handleNavClick}>Detections</NavItem>
          </>}

          {/* Insights: numbers. Folded until opened. */}
          {adv && ((lic.isPro && (canAccessPage('analytics') || canAccessPage('education_reports'))) || canAccessPage('calculator')) && <NavGroup label={t('nav.insights')} collapsed={c} open={sections.insights} onToggle={() => toggle('insights')} />}
          {adv && (c || sections.insights) && <>
            {lic.isPro && canAccessPage('analytics') && <NavItem collapsed={c} to="/analytics" icon={BarChart3} onClick={handleNavClick}>{t('nav.analytics')}</NavItem>}
            {lic.isPro && canAccessPage('analytics') && <NavItem collapsed={c} to="/utilization" icon={Activity} onClick={handleNavClick}>Utilization</NavItem>}
            {lic.isPro && canAccessPage('education_reports') && <NavItem collapsed={c} to="/education-reports" icon={BarChart3} onClick={handleNavClick}>Usage Reports</NavItem>}
            {canAccessPage('calculator') && <NavItem collapsed={c} to="/calculator" icon={Calculator} onClick={handleNavClick}>{t('nav.calculator')}</NavItem>}
          </>}

          {/* Settings (the audit log is reached from Settings > System) */}
          <div className="mt-2" style={{ borderTop: '1px solid var(--brand-sidebar-border)', paddingTop: '0.5rem' }}>
            {canAccessPage('settings') && <NavItem collapsed={c} to="/settings" icon={Settings} onClick={handleNavClick}>{t('nav.settings')}</NavItem>}
          </div>
        </nav>

        {/* Who is signed in, and the way out */}
        <div className="shrink-0 px-3 py-2.5 flex items-center gap-2.5" style={{ borderTop: '1px solid var(--brand-sidebar-border)' }}>
          {!c && principal?.username && (
            <>
              <span aria-hidden="true" className="grid h-7 w-7 shrink-0 place-items-center rounded-full text-xs font-semibold uppercase"
                style={{ backgroundColor: 'var(--brand-sidebar-active-bg)', color: 'var(--brand-text-primary)' }}>{principal.username.slice(0, 1)}</span>
              <span className="min-w-0 leading-tight">
                <span className="block truncate text-sm" style={{ color: 'var(--brand-text-primary)' }}>{principal.username}</span>
                {principal.role && <span className="block text-[11px] capitalize" style={{ color: 'var(--brand-text-muted)' }}>{principal.role}</span>}
              </span>
            </>
          )}
          <button
            onClick={() => setShowLogoutConfirm(true)}
            className={clsx("flex items-center gap-1.5 hover:text-red-400 text-xs transition-colors", c ? "mx-auto" : "ml-auto")}
            style={{ color: 'var(--brand-sidebar-text)' }}
            aria-label="Sign out"
          >
            <LogOut size={14} />
            {!c && "Sign out"}
          </button>
        </div>
      </aside>

      <Modal isOpen={showLogoutConfirm} onClose={() => setShowLogoutConfirm(false)} title="Sign out?" size="sm" alert>
        <p className="text-sm mb-4" style={{ color: 'var(--brand-text-secondary)' }}>Sign out of ODIN on this device?</p>
        <div className="flex justify-end gap-2">
          <Button variant="secondary" size="sm" onClick={() => setShowLogoutConfirm(false)}>Cancel</Button>
          <Button variant="danger" size="sm" onClick={async () => { await fetch('/api/auth/logout', { method: 'POST', credentials: 'include' }).catch(() => {}); await clearSensitiveBrowserState(); window.location.href = '/login'; }}>Sign out</Button>
        </div>
      </Modal>
    </>
  )
}
