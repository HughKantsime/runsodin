import { useId, useState } from 'react'
import { Palette } from 'lucide-react'
import { useTheme } from '../../hooks/useTheme'
import type { ThemeFamily, ThemeMode } from '../../hooks/themePreferences'

export default function ThemeToggle() {
  const { theme, family, setFamily, setTheme, effectiveFamily } = useTheme()
  const [open, setOpen] = useState(false)
  const id = useId()
  const fallback = family === 'foundry' && effectiveFamily === 'classic'
  return (
    <div className="theme-settings relative flex items-center gap-1">
      <button aria-label="Theme settings" aria-expanded={open} aria-controls={id}
        onClick={() => setOpen(!open)}
        className="p-1.5 rounded-md text-[var(--brand-text-secondary)] hover:bg-[var(--brand-surface)] min-w-[32px] min-h-[32px] flex items-center justify-center">
        <Palette size={18} />
      </button>
      {fallback && <span className="sr-only" role="status">Foundry is dark only. Using Classic {theme === 'light' ? 'light' : 'high-contrast'}.</span>}
      {open && <div id={id} className="theme-settings-panel absolute right-0 top-full mt-2 z-50 w-64 max-w-[calc(100vw-2rem)] p-4 rounded-lg border border-[var(--brand-card-border)] bg-[var(--brand-card-bg)] text-[var(--brand-text-primary)] shadow-lg space-y-3"
        onKeyDown={event => { if (event.key === 'Escape') { setOpen(false); event.currentTarget.parentElement?.querySelector<HTMLButtonElement>('[aria-label="Theme settings"]')?.focus() } }}>
        <label className="block text-sm">Theme family
          <select className="block w-full mt-1 p-2" value={family} onChange={e => setFamily(e.target.value as ThemeFamily)}>
            <option value="classic">Classic</option><option value="foundry">Foundry</option>
          </select>
        </label>
        <label className="block text-sm">Appearance
          <select className="block w-full mt-1 p-2" value={theme} onChange={e => setTheme(e.target.value as ThemeMode)}>
            <option value="dark">Dark</option><option value="light">Light</option><option value="high-contrast">High contrast</option>
          </select>
        </label>
        {fallback && <p className="text-sm text-[var(--brand-text-secondary)]">Foundry is dark only. Using Classic {theme === 'light' ? 'light' : 'high-contrast'}. Choose Dark to return to Foundry.</p>}
        <button className="text-sm underline" onClick={() => setOpen(false)}>Close theme settings</button>
      </div>}
    </div>
  )
}
