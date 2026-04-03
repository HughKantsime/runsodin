import { type ReactNode } from 'react'
import { type LucideIcon } from 'lucide-react'

interface EmptyStateProps {
  icon?: LucideIcon
  title: string
  description?: string
  children?: ReactNode
}

export default function EmptyState({ icon: Icon, title, description, children }: EmptyStateProps) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      {Icon && <Icon size={32} className="text-[var(--brand-text-muted)] mb-3" />}
      <h3 className="font-semibold text-sm text-[var(--brand-text-primary)] mb-1">{title}</h3>
      {description && <p className="text-xs text-[var(--brand-text-secondary)] max-w-sm">{description}</p>}
      {children && <div className="mt-4">{children}</div>}
    </div>
  )
}
