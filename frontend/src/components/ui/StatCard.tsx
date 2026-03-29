import { type ReactNode } from 'react'
import { type LucideIcon } from 'lucide-react'
import clsx from 'clsx'

type AccentColor = 'green' | 'blue' | 'amber' | 'red' | 'purple' | 'default'

interface StatCardProps {
  icon?: LucideIcon
  label: string
  value: ReactNode
  subtitle?: string
  color?: AccentColor
  onClick?: () => void
  className?: string
}

const accentColors: Record<string, string> = {
  green: 'border-l-[var(--status-completed)]',
  blue: 'border-l-[var(--status-printing)]',
  amber: 'border-l-[var(--brand-primary)]',
  red: 'border-l-[var(--status-failed)]',
  purple: 'border-l-[var(--status-scheduled)]',
  default: 'border-l-transparent',
}

export default function StatCard({ icon: Icon, label, value, subtitle, color = 'default', onClick, className }: StatCardProps) {
  return (
    <div
      className={clsx(
        'bg-[var(--brand-card-bg)] rounded-md p-4 border-l-2 transition-colors',
        accentColors[color] || accentColors.default,
        onClick && 'cursor-pointer hover:brightness-110',
        className
      )}
      style={{ boxShadow: 'var(--brand-card-shadow)' }}
      onClick={onClick}
    >
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs font-medium text-[var(--brand-text-muted)] uppercase tracking-wider">{label}</span>
        {Icon && <Icon size={16} className="text-[var(--brand-text-muted)]" />}
      </div>
      <div className="font-mono font-semibold text-xl text-[var(--brand-text-primary)]">{value}</div>
      {subtitle && <div className="text-xs text-[var(--brand-text-secondary)] mt-1">{subtitle}</div>}
    </div>
  )
}
