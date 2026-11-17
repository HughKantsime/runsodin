import { useId, useState } from 'react'
import { Palette, Sun, Moon } from 'lucide-react'
import { useTheme } from '../../hooks/useTheme'
import ThemePicker from './ThemePicker'

export default function ThemeToggle() {
  const { theme, family, effectiveFamily } = useTheme()
  const [open, setOpen] = useState(false)
  const id = useId()
  const fallback = family === 'foundry' && effectiveFamily === 'classic'
  return (
    <div className={`theme-settings relative ${open ? 'flex items-center' : 'contents'}`}>
      <button aria-label="Theme settings" aria-expanded={open} aria-controls={id}
        onClick={() => setOpen(!open)}
        className="text-[var(--brand-text-secondary)] hover:text-[var(--brand-text-primary)] hover:bg-[var(--brand-surface)] p-1.5 rounded-md transition-colors min-w-[32px] min-h-[32px] flex items-center justify-center">
        {effectiveFamily === 'foundry' ? <Palette size={18} /> : theme === 'dark' ? <Sun size={18} /> : <Moon size={18} />}
      </button>
      {fallback && <span className="sr-only" role="status">Foundry is dark only. Using Classic {theme === 'light' ? 'light' : 'high-contrast'}.</span>}
      {open && <div id={id} className="theme-settings-panel absolute right-0 top-full mt-2 z-50 w-64 max-w-[calc(100vw-2rem)] p-4 rounded-lg border border-[var(--brand-card-border)] bg-[var(--brand-card-bg)] text-[var(--brand-text-primary)] shadow-lg space-y-3"
        onKeyDown={event => { if (event.key === 'Escape') { setOpen(false); event.currentTarget.parentElement?.querySelector<HTMLButtonElement>('[aria-label="Theme settings"]')?.focus() } }}>
        <ThemePicker />
        <button className="text-sm underline" onClick={() => setOpen(false)}>Close theme settings</button>
      </div>}
    </div>
  )
}
