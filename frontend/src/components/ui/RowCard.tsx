import { Fragment } from 'react'
import type { ReactNode } from 'react'

/** Phone layout (< 640 px) for tables: one stacked card per row. Name first, then status, fields, actions. */
export function RowCardList({ label, children }: { label: string; children: ReactNode }) {
  return (
    <ul aria-label={label} className="rounded-md border border-[var(--brand-card-border)] bg-[var(--brand-card-bg)] overflow-hidden">
      {children}
    </ul>
  )
}

export interface RowCardProps {
  title: ReactNode
  /** Makes the title a button (44 px tall) that opens the row. */
  onOpen?: () => void
  leading?: ReactNode
  status?: ReactNode
  /** [label, value] pairs; null / undefined / false / '' values are skipped. */
  fields?: Array<[string, ReactNode]>
  children?: ReactNode
  actions?: ReactNode
}

export function RowCard({ title, onOpen, leading, status, fields = [], children, actions }: RowCardProps) {
  const shown = fields.filter(([, value]) => value !== null && value !== undefined && value !== false && value !== '')
  return (
    <li data-testid="row-card" className="px-3 py-3 border-b border-[var(--brand-card-border)] last:border-b-0">
      <div className="flex items-start gap-2">
        {leading}
        <div className="min-w-0 flex-1">
          {onOpen
            ? <button type="button" onClick={onOpen} className="min-h-11 w-full text-left font-medium text-sm break-words">{title}</button>
            : <div className="font-medium text-sm break-words">{title}</div>}
          {children}
        </div>
      </div>
      {status && <div className="mt-2 flex flex-wrap items-center gap-2">{status}</div>}
      {shown.length > 0 && (
        <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
          {shown.map(([label, value]) => (
            <Fragment key={label}>
              <dt className="text-xs text-[var(--brand-text-muted)]">{label}</dt>
              <dd className="min-w-0 break-words text-[var(--brand-text-secondary)]">{value}</dd>
            </Fragment>
          ))}
        </dl>
      )}
      {actions && <div className="mt-2 flex flex-wrap items-center gap-1 [&_button]:min-h-11 [&_button]:min-w-11">{actions}</div>}
    </li>
  )
}
