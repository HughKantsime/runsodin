import { Sun, Moon } from 'lucide-react'
import { useTheme } from '../../hooks/useTheme'

export default function ThemeToggle() {
  const { theme, toggleTheme } = useTheme()
  return (
    <button
      onClick={toggleTheme}
      className="text-[var(--brand-text-secondary)] hover:text-[var(--brand-text-primary)] hover:bg-[var(--brand-surface)] p-1.5 rounded-md transition-colors min-w-[32px] min-h-[32px] flex items-center justify-center"
      aria-label={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
    >
      {theme === 'dark' ? <Sun size={18} /> : <Moon size={18} />}
    </button>
  )
}
