import clsx from 'clsx'

interface StatusStyle {
  dot: string
  text: string
  bg: string
}

interface StatusBadgeProps {
  status: string
  size?: 'sm' | 'md'
  variant?: 'inline' | 'badge'
}

const statusStyles: Record<string, StatusStyle> = {
  pending:    { dot: 'bg-[var(--status-pending)]',    text: 'text-[var(--status-pending)]',    bg: 'bg-[var(--status-pending)]' },
  queued:     { dot: 'bg-[var(--status-pending)]',    text: 'text-[var(--status-pending)]',    bg: 'bg-[var(--status-pending)]' },
  scheduled:  { dot: 'bg-[var(--status-scheduled)]',  text: 'text-[var(--status-scheduled)]',  bg: 'bg-[var(--status-scheduled)]' },
  printing:   { dot: 'bg-[var(--status-printing)]',   text: 'text-[var(--status-printing)]',   bg: 'bg-[var(--status-printing)]' },
  running:    { dot: 'bg-[var(--status-printing)]',   text: 'text-[var(--status-printing)]',   bg: 'bg-[var(--status-printing)]' },
  paused:     { dot: 'bg-[var(--status-warning)]',    text: 'text-[var(--status-warning)]',    bg: 'bg-[var(--status-warning)]' },
  completed:  { dot: 'bg-[var(--status-completed)]',  text: 'text-[var(--status-completed)]',  bg: 'bg-[var(--status-completed)]' },
  done:       { dot: 'bg-[var(--status-completed)]',  text: 'text-[var(--status-completed)]',  bg: 'bg-[var(--status-completed)]' },
  failed:     { dot: 'bg-[var(--status-failed)]',     text: 'text-[var(--status-failed)]',     bg: 'bg-[var(--status-failed)]' },
  error:      { dot: 'bg-[var(--status-failed)]',     text: 'text-[var(--status-failed)]',     bg: 'bg-[var(--status-failed)]' },
  cancelled:  { dot: 'bg-[var(--status-pending)]',    text: 'text-[var(--status-pending)]',    bg: 'bg-[var(--status-pending)]' },
  rejected:   { dot: 'bg-[var(--status-failed)]',     text: 'text-[var(--status-failed)]',     bg: 'bg-[var(--status-failed)]' },
  approved:   { dot: 'bg-[var(--status-completed)]',  text: 'text-[var(--status-completed)]',  bg: 'bg-[var(--status-completed)]' },
}

const defaultStyle: StatusStyle = { dot: 'bg-[var(--status-pending)]', text: 'text-[var(--status-pending)]', bg: 'bg-[var(--status-pending)]' }

function normalizeStatus(status: string): string {
  return (status || 'pending').toLowerCase().replace(/[-_\s]/g, '')
}

export default function StatusBadge({ status, size = 'sm', variant = 'inline' }: StatusBadgeProps) {
  const key = normalizeStatus(status)
  const style = statusStyles[key] || defaultStyle
  const label = (status || 'pending').replace(/[-_]/g, ' ')
  const isPrinting = key === 'printing' || key === 'running'

  if (variant === 'badge') {
    return (
      <span className={clsx(
        'inline-flex items-center gap-1.5 rounded-sm px-2 py-0.5',
        style.bg + '/8',
        style.text,
        size === 'sm' ? 'text-xs' : 'text-sm'
      )}>
        <span className={clsx('w-1.5 h-1.5 rounded-full flex-shrink-0', style.dot, isPrinting && 'animate-pulse')} />
        <span className="capitalize font-medium">{label}</span>
      </span>
    )
  }

  return (
    <span className={clsx(
      'inline-flex items-center gap-1.5',
      style.text,
      size === 'sm' ? 'text-xs' : 'text-sm'
    )}>
      <span className={clsx('w-1.5 h-1.5 rounded-full flex-shrink-0', style.dot, isPrinting && 'animate-pulse')} />
      <span className="capitalize font-medium">{label}</span>
    </span>
  )
}
