import { useEffect, useState } from 'react'
import { brandingColors, useBranding } from '../../BrandingContext'
import { useTheme } from '../../hooks/useTheme'
import type { ThemeFamily, ThemeMode } from '../../hooks/themePreferences'

export default function ThemePicker() {
  const { theme, family, setFamily, setTheme, effectiveFamily, density, setDensity } = useTheme()
  const branding = useBranding()
  const [userAccent, setUserAccent] = useState(document.documentElement.dataset.userAccent)
  useEffect(() => {
    const observer = new MutationObserver(() => setUserAccent(document.documentElement.dataset.userAccent))
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-user-accent'] })
    return () => observer.disconnect()
  }, [])
  return <>
    <label className="block text-sm">Theme family
      <select className="block w-full mt-1 p-2" value={family} onChange={e => setFamily(e.target.value as ThemeFamily)}>
        <option value="classic">Classic</option><option value="foundry">Foundry</option><option value="fjord">Fjord</option>
      </select>
    </label>
    <label className="block text-sm">Appearance
      <select className="block w-full mt-1 p-2" value={theme} onChange={e => setTheme(e.target.value as ThemeMode)}>
        <option value="dark">Dark</option><option value="light">Light</option><option value="high-contrast">High contrast</option>
      </select>
    </label>
    {family === 'foundry' && effectiveFamily === 'classic' && <p className="text-sm text-[var(--brand-text-secondary)]">Foundry is dark only. Using Classic {theme === 'light' ? 'light' : 'high-contrast'}. Choose Dark to return to Foundry.</p>}
    {family === 'fjord' && <>
      <div role="img" aria-label="Fjord preview: light and dark" className="flex gap-2">
        {['light', 'dark'].map(mode => {
          const colors = brandingColors(branding, mode === 'light', true, userAccent)
          return <div key={mode} className="flex-1 p-3" style={{ background: mode === 'light' ? '#f5f5f7' : '#0b0b0e', color: mode === 'light' ? '#1d1d1f' : '#f5f5f7', borderRadius: 16 }}>
          <span className="text-xs">{mode === 'light' ? 'Light' : 'Dark'}</span>
          <div className="mt-2 h-8 border" style={{ background: mode === 'light' ? '#ffffff' : '#18181c', borderColor: mode === 'light' ? '#e4e4e6' : '#2d2d30', borderRadius: 12 }} />
          <div aria-label={`Fjord ${mode} primary`} className="mt-2 px-2 py-1 text-xs rounded-full" style={{ background: colors['--brand-primary'] || (mode === 'light' ? '#9a5b00' : '#ffb340'), color: colors['--brand-on-primary'] || (mode === 'light' ? '#ffffff' : '#1d1d1f') }}>Primary</div>
          <div role="img" aria-label={`Fjord ${mode} accent`} className="mt-2 h-2 w-8 rounded-full" style={{ background: colors['--brand-accent'] || (mode === 'light' ? '#f5a524' : '#ffb340') }} />
        </div>})}
      </div>
      <label className="block text-sm">Density
        <select className="block w-full mt-1 p-2" value={density} onChange={e => setDensity(e.target.value as 'comfortable' | 'compact')}>
          <option value="comfortable">Comfortable</option><option value="compact">Compact</option>
        </select>
      </label>
      {effectiveFamily === 'classic' && <p role="status" className="text-sm text-[var(--brand-text-secondary)]">Using Classic high contrast. Choose Light or Dark to return to Fjord.</p>}
    </>}
  </>
}
