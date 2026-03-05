import clsx from 'clsx'

const colorStyles = {
  green: 'var(--status-completed)',
  red: 'var(--status-failed)',
  yellow: 'bg-yellow-500',
  blue: 'var(--status-printing)',
  print: 'var(--brand-primary)',
}

const colorClasses = {
  yellow: 'bg-yellow-500',
}

const sizeMap = {
  sm: 'h-1',
  md: 'h-2.5',
}

export default function ProgressBar({
  value,
  color = 'print',
  size = 'sm',
  className,
}) {
  const clamped = Math.max(0, Math.min(100, value || 0))

  return (
    <div
      className={clsx(
        'w-full bg-[var(--brand-input-bg)] rounded-full',
        sizeMap[size],
        className
      )}
      role="progressbar"
      aria-valuenow={clamped}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      <div
        className={clsx(
          'rounded-full transition-all duration-500',
          sizeMap[size],
          colorClasses[color]
        )}
        style={{
          width: `${clamped}%`,
          backgroundColor: colorClasses[color] ? undefined : (colorStyles[color] || colorStyles.print),
        }}
      />
    </div>
  )
}
