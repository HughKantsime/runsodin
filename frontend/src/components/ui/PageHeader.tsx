import { type ReactNode } from 'react'
import { type LucideIcon, HelpCircle } from 'lucide-react'
import { useLocation } from 'react-router-dom'
import { getHelpLink } from '../../utils/helpLinks'

interface PageHeaderProps {
  icon?: LucideIcon
  title: string
  subtitle?: string
  children?: ReactNode
  helpUrl?: string
}

export default function PageHeader({ icon: Icon, title, subtitle, children, helpUrl }: PageHeaderProps) {
  const { pathname } = useLocation()
  const autoHelp = getHelpLink(pathname)
  const finalHelpUrl = helpUrl || autoHelp?.url
  const helpLabel = autoHelp?.label || 'Documentation'

  return (
    <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-6">
      <div className="flex items-center gap-3">
        {Icon && <Icon size={20} className="text-[var(--brand-primary)] flex-shrink-0" />}
        <div>
          <h1 className="font-display font-semibold text-xl text-[var(--brand-text-primary)] tracking-tight">{title}</h1>
          {subtitle && <p className="text-xs text-[var(--brand-text-secondary)] mt-0.5">{subtitle}</p>}
        </div>
        {finalHelpUrl && (
          <a
            href={finalHelpUrl}
            target="_blank"
            rel="noopener noreferrer"
            title={helpLabel}
            className="text-[var(--brand-text-muted)] hover:text-[var(--brand-primary)] transition-colors flex-shrink-0"
          >
            <HelpCircle size={16} />
          </a>
        )}
      </div>
      {children && <div className="flex items-center gap-2 flex-wrap">{children}</div>}
    </div>
  )
}
