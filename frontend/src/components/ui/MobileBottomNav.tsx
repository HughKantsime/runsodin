import { useState } from 'react'
import { NavLink, useLocation } from 'react-router-dom'
import {
  LayoutDashboard,
  Printer,
  ListTodo,
  Video,
  Menu,
  X,
  Circle,
  BarChart3,
  Settings,
  Upload,
  Calendar,
  ShoppingCart,
  Box,
  Wrench,
  Archive,
  Bell,
  Eye,
} from 'lucide-react'
import clsx from 'clsx'

const PRIMARY_TABS = [
  { to: '/', icon: LayoutDashboard, label: 'Dashboard' },
  { to: '/printers', icon: Printer, label: 'Printers' },
  { to: '/jobs', icon: ListTodo, label: 'Jobs' },
  { to: '/cameras', icon: Video, label: 'Cameras' },
]

const MORE_ITEMS = [
  { to: '/upload', icon: Upload, label: 'Upload' },
  { to: '/timeline', icon: Calendar, label: 'Timeline' },
  { to: '/orders', icon: ShoppingCart, label: 'Orders' },
  { to: '/models', icon: Box, label: 'Models' },
  { to: '/spools', icon: Circle, label: 'Spools' },
  { to: '/alerts', icon: Bell, label: 'Alerts' },
  { to: '/detections', icon: Eye, label: 'Detections' },
  { to: '/archives', icon: Archive, label: 'Archives' },
  { to: '/analytics', icon: BarChart3, label: 'Analytics' },
  { to: '/maintenance', icon: Wrench, label: 'Maintenance' },
  { to: '/settings', icon: Settings, label: 'Settings' },
]

export default function MobileBottomNav() {
  const [moreOpen, setMoreOpen] = useState(false)
  const { pathname } = useLocation()

  // Check if current path is in the "more" menu
  const isMoreActive = MORE_ITEMS.some(item => pathname === item.to || pathname.startsWith(item.to + '/'))

  return (
    <>
      {/* More menu overlay */}
      {moreOpen && (
        <div className="fixed inset-0 z-40 md:hidden" onClick={() => setMoreOpen(false)}>
          <div className="absolute inset-0 bg-black/50" />
          <div
            className="absolute bottom-16 left-0 right-0 bg-[var(--brand-card-bg)] border-t border-[var(--brand-card-border)] rounded-t-xl p-3 max-h-[60vh] overflow-y-auto"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between mb-3 px-1">
              <span className="text-xs font-semibold text-[var(--brand-text-secondary)] uppercase tracking-wider">More</span>
              <button onClick={() => setMoreOpen(false)} className="p-1 text-[var(--brand-text-muted)]">
                <X size={16} />
              </button>
            </div>
            <div className="grid grid-cols-3 gap-1">
              {MORE_ITEMS.map(({ to, icon: Icon, label }) => (
                <NavLink
                  key={to}
                  to={to}
                  onClick={() => setMoreOpen(false)}
                  className={({ isActive }) => clsx(
                    'flex flex-col items-center gap-1.5 py-3 px-2 rounded-lg text-center transition-colors',
                    isActive
                      ? 'bg-[var(--brand-sidebar-active-bg)] text-[var(--brand-primary)]'
                      : 'text-[var(--brand-text-secondary)] active:bg-[var(--brand-surface)]'
                  )}
                  style={{ touchAction: 'manipulation' }}
                >
                  <Icon size={20} />
                  <span className="text-[10px] font-medium leading-tight">{label}</span>
                </NavLink>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* Bottom nav bar */}
      <nav
        className="fixed bottom-0 left-0 right-0 z-30 md:hidden bg-[var(--brand-sidebar-bg)] border-t border-[var(--brand-sidebar-border)]"
        style={{ touchAction: 'manipulation', paddingBottom: 'env(safe-area-inset-bottom)' }}
      >
        <div className="flex items-stretch justify-around h-14">
          {PRIMARY_TABS.map(({ to, icon: Icon, label }) => (
            <NavLink
              key={to}
              to={to}
              end={to === '/'}
              className={({ isActive }) => clsx(
                'flex flex-col items-center justify-center flex-1 gap-0.5 transition-colors',
                isActive
                  ? 'text-[var(--brand-primary)]'
                  : 'text-[var(--brand-text-muted)]'
              )}
            >
              <Icon size={20} />
              <span className="text-[10px] font-medium">{label}</span>
            </NavLink>
          ))}
          <button
            onClick={() => setMoreOpen(!moreOpen)}
            className={clsx(
              'flex flex-col items-center justify-center flex-1 gap-0.5 transition-colors',
              moreOpen || isMoreActive
                ? 'text-[var(--brand-primary)]'
                : 'text-[var(--brand-text-muted)]'
            )}
          >
            <Menu size={20} />
            <span className="text-[10px] font-medium">More</span>
          </button>
        </div>
      </nav>
    </>
  )
}
