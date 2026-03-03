import { Menu } from 'lucide-react'
import { useBranding } from '../../BrandingContext'
import GlobalSearch from '../shared/GlobalSearch'
import ThemeToggle from './ThemeToggle'
import AlertBell from '../notifications/AlertBell'

export default function MobileHeader({ onMenuClick }) {
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
