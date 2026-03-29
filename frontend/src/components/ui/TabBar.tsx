import { type LucideIcon } from 'lucide-react'
import clsx from 'clsx'

interface Tab {
  key?: string
  value?: string
  label?: string
  icon?: LucideIcon
  count?: number
}

interface TabBarProps {
  tabs: (Tab | string)[]
  activeTab?: string
  active?: string
  onTabChange?: (key: string) => void
  onChange?: (key: string) => void
  variant?: 'default' | 'inline' | 'segment' | 'pill'
}

export default function TabBar({ tabs, activeTab, active, onTabChange, onChange, variant = 'default' }: TabBarProps) {
  // Backward compatibility: support old prop names
  const currentTab = activeTab ?? active
  const handleChange = onTabChange || onChange
  // Map old variant names
  const resolvedVariant = variant === 'segment' ? 'default' : variant === 'pill' ? 'default' : variant

  return (
    <div className={clsx(
      'flex gap-1',
      resolvedVariant === 'inline' ? 'border-b border-[var(--brand-card-border)]' : 'bg-[var(--brand-card-bg)] rounded-md p-1'
    )}>
      {tabs.map(tab => {
        const key = typeof tab === 'string' ? tab : (tab.key || tab.value || tab.label || '')
        const label = typeof tab === 'string' ? tab : (tab.label || '')
        const Icon = typeof tab === 'string' ? undefined : tab.icon
        const count = typeof tab === 'string' ? undefined : tab.count
        const isActive = currentTab === key

        return (
          <button
            key={key}
            onClick={() => handleChange?.(key)}
            className={clsx(
              'flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium transition-colors rounded-sm',
              resolvedVariant === 'inline'
                ? isActive
                  ? 'text-[var(--brand-primary)] border-b-2 border-[var(--brand-primary)] -mb-px'
                  : 'text-[var(--brand-text-secondary)] hover:text-[var(--brand-text-primary)]'
                : isActive
                  ? 'bg-[var(--brand-primary)] text-white'
                  : 'text-[var(--brand-text-secondary)] hover:text-[var(--brand-text-primary)] hover:bg-[var(--brand-surface)]'
            )}
          >
            {Icon && <Icon size={14} />}
            {label}
            {count != null && (
              <span className={clsx(
                'text-[10px] px-1.5 py-0.5 rounded-sm font-mono',
                isActive ? 'bg-white/20' : 'bg-[var(--brand-surface)] text-[var(--brand-text-muted)]'
              )}>{count}</span>
            )}
          </button>
        )
      })}
    </div>
  )
}
