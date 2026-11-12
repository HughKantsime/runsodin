import { useInRouterContext, useLocation } from 'react-router-dom'
import { useFoundry } from '../../hooks/themePreferences'
import { useEffect, useRef, useState } from 'react'
import { Menu, Search } from 'lucide-react'
import { useMediaQuery } from '../../hooks/useMediaQuery'
import { useBranding } from '../../BrandingContext'
import GlobalSearch from '../shared/GlobalSearch'
import ThemeToggle from './ThemeToggle'
import AlertBell from '../notifications/AlertBell'

/** Runs `onChange` when the route changes (rendered only inside a router). */
function OnRouteChange({ onChange }: { onChange: () => void }) {
  const { pathname } = useLocation()
  useEffect(() => { onChange() }, [pathname])  // eslint-disable-line react-hooks/exhaustive-deps
  return null
}

export default function MobileHeader({ onMenuClick }) {
  const branding = useBranding()
  const foundry = useFoundry()
  // Under 640 px the search collapses to an icon button; tapping it expands the field.
  const phone = useMediaQuery('(max-width: 639px)')
  const [searchOpen, setSearchOpen] = useState(false)
  const expanded = phone && searchOpen
  const searchBox = useRef<HTMLDivElement>(null)
  const searchButton = useRef<HTMLButtonElement>(null)
  const refocusButton = useRef(false)
  useEffect(() => {
    if (expanded) searchBox.current?.querySelector('input')?.focus()
  }, [expanded])
  // Picking a result navigates: collapse the field again, and never re-open it after a rotation.
  const inRouter = useInRouterContext()
  useEffect(() => { setSearchOpen(false) }, [phone])
  useEffect(() => {
    if (!searchOpen && refocusButton.current) {
      refocusButton.current = false
      searchButton.current?.focus()
    }
  }, [searchOpen])

  return (
    <header
      className={foundry ? 'mobile-header md:hidden flex items-center justify-between gap-2 p-4 shrink-0' : 'mobile-header md:hidden flex items-center justify-between p-4 shrink-0'}
      style={{
        backgroundColor: 'var(--brand-sidebar-bg)',
        borderBottom: '1px solid var(--brand-sidebar-border)',
      }}
    >
      {inRouter && <OnRouteChange onChange={() => setSearchOpen(false)} />}
      <div className={(foundry ? 'flex items-center gap-3 min-w-0 shrink-0 max-w-[28%]' : 'flex items-center gap-3') + (expanded ? ' hidden' : '')}>
        {branding.logo_url && (
          <img src={branding.logo_url} alt={branding.app_name} className={foundry ? 'h-6 max-w-8 object-contain shrink-0' : 'h-6 object-contain'} />
        )}
        <h1 className={foundry ? 'text-sm font-display font-bold truncate' : 'text-sm font-display font-bold'} style={{ color: 'var(--brand-text-primary)' }}>
          {branding.app_name}
        </h1>
      </div>
      <div className={(foundry ? 'mobile-header-actions flex flex-1 min-w-0 items-center justify-end gap-2' : 'flex items-center gap-2') + (expanded ? ' w-full' : '')}>
        {!phone ? (
          <GlobalSearch id="global-search-mobile" />
        ) : expanded ? (
          <div
            ref={searchBox}
            className="flex-1 min-w-0 [&>div]:w-full [&_input]:w-full"
            onKeyDown={e => { if (e.key === 'Escape') { refocusButton.current = true; setSearchOpen(false) } }}
            onBlur={e => {
              const next = e.relatedTarget as Node | null
              if (next && e.currentTarget.contains(next)) return
              if (!e.currentTarget.querySelector('input')?.value) setSearchOpen(false)
            }}
          >
            <GlobalSearch id="global-search-mobile" />
          </div>
        ) : (
          <button
            ref={searchButton}
            type="button"
            onClick={() => setSearchOpen(true)}
            className="p-2 rounded-lg transition-colors min-w-11 min-h-11 flex items-center justify-center"
            style={{ color: 'var(--brand-sidebar-text)' }}
            aria-label="Search"
            aria-expanded={false}
          >
            <Search size={20} aria-hidden="true" />
          </button>
        )}
        <ThemeToggle />
        <AlertBell />
        <button
          onClick={onMenuClick}
          className="p-2 rounded-lg transition-colors min-w-[32px] min-h-[32px] flex items-center justify-center max-sm:min-w-11 max-sm:min-h-11"
          style={{ color: 'var(--brand-sidebar-text)' }}
          aria-label="Open menu"
        >
          <Menu size={20} />
        </button>
      </div>
    </header>
  )
}
