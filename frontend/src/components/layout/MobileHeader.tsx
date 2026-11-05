import { Menu } from 'lucide-react'
import { useBranding } from '../../BrandingContext'
import GlobalSearch from '../shared/GlobalSearch'
import ThemeToggle from './ThemeToggle'
import AlertBell from '../notifications/AlertBell'

export default function MobileHeader({ onMenuClick }) {
  const branding = useBranding()

  return (
    <header
      className="mobile-header md:hidden flex items-center justify-between gap-2 p-4 shrink-0"
      style={{
        backgroundColor: 'var(--brand-sidebar-bg)',
        borderBottom: '1px solid var(--brand-sidebar-border)',
      }}
    >
      <div className="flex items-center gap-3 min-w-0 shrink-0 max-w-[28%]">
        {branding.logo_url && (
          <img src={branding.logo_url} alt={branding.app_name} className="h-6 max-w-8 object-contain shrink-0" />
        )}
        <h1 className="text-sm font-display font-bold truncate" style={{ color: 'var(--brand-text-primary)' }}>
          {branding.app_name}
        </h1>
      </div>
      <div className="mobile-header-actions flex flex-1 min-w-0 items-center justify-end gap-2">
        <GlobalSearch id="global-search-mobile" />
        <ThemeToggle />
        <AlertBell />
        <button
          onClick={onMenuClick}
          className="p-2 rounded-lg transition-colors min-w-[32px] min-h-[32px] flex items-center justify-center"
          style={{ color: 'var(--brand-sidebar-text)' }}
          aria-label="Open menu"
        >
          <Menu size={20} />
        </button>
      </div>
    </header>
  )
}
