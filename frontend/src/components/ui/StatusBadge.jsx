import clsx from 'clsx'

const statusMap = {
  pending:     { bg: 'bg-status-pending/20',   text: 'text-gray-400',   dot: 'bg-gray-400' },
  queued:      { bg: 'bg-status-pending/20',   text: 'text-gray-400',   dot: 'bg-gray-400' },
  scheduled:   { bg: 'bg-status-scheduled/20', text: 'text-violet-400', dot: 'bg-violet-400' },
  printing:    { bg: 'bg-status-printing/20',  text: 'text-blue-400',   dot: 'bg-blue-400' },
  in_progress: { bg: 'bg-status-printing/20',  text: 'text-blue-400',   dot: 'bg-blue-400' },
  active:      { bg: 'bg-status-printing/20',  text: 'text-blue-400',   dot: 'bg-blue-400' },
  completed:   { bg: 'bg-status-completed/20', text: 'text-green-400',  dot: 'bg-green-400' },
  done:        { bg: 'bg-status-completed/20', text: 'text-green-400',  dot: 'bg-green-400' },
  delivered:   { bg: 'bg-status-completed/20', text: 'text-green-400',  dot: 'bg-green-400' },
  shipped:     { bg: 'bg-status-completed/20', text: 'text-green-400',  dot: 'bg-green-400' },
  fulfilled:   { bg: 'bg-status-completed/20', text: 'text-green-400',  dot: 'bg-green-400' },
  failed:      { bg: 'bg-status-failed/20',    text: 'text-red-400',    dot: 'bg-red-400' },
  error:       { bg: 'bg-status-failed/20',    text: 'text-red-400',    dot: 'bg-red-400' },
  rejected:    { bg: 'bg-status-failed/20',    text: 'text-red-400',    dot: 'bg-red-400' },
  cancelled:   { bg: 'bg-amber-400/20',        text: 'text-amber-400',  dot: 'bg-amber-400' },
  submitted:   { bg: 'bg-farm-600/20',         text: 'text-farm-400',   dot: 'bg-farm-400' },
  draft:       { bg: 'bg-farm-600/20',         text: 'text-farm-400',   dot: 'bg-farm-400' },
  paused:      { bg: 'bg-yellow-400/20',       text: 'text-yellow-400', dot: 'bg-yellow-400' },
}

const defaultColors = { bg: 'bg-farm-600/20', text: 'text-farm-400', dot: 'bg-farm-400' }

export default function StatusBadge({ status, size = 'md' }) {
  const normalized = (status || '').toLowerCase().replace(/\s+/g, '_')
  const colors = statusMap[normalized] || defaultColors

  const label = (status || '')
    .replace(/_/g, ' ')
    .replace(/\b\w/g, c => c.toUpperCase())

  return (
    <span
      className={clsx(
        'inline-flex items-center gap-1.5 rounded-full font-medium',
        colors.bg,
        colors.text,
        size === 'sm' ? 'px-1.5 py-0.5 text-[10px]' : 'px-2 py-0.5 text-xs'
      )}
    >
      <span
        className={clsx(
          'rounded-full',
          colors.dot,
          size === 'sm' ? 'w-1 h-1' : 'w-1.5 h-1.5'
        )}
      />
      {label}
    </span>
  )
}
