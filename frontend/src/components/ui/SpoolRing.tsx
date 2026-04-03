import clsx from 'clsx'

interface SpoolRingProps {
  color?: string
  material?: string
  level?: number
  empty?: boolean
  size?: number
  className?: string
}

export default function SpoolRing({ color = '#888', material = '', level = 100, empty = false, size = 20, className }: SpoolRingProps) {
  const strokeWidth = 3
  const radius = (size - strokeWidth) / 2
  const circumference = 2 * Math.PI * radius
  const clampedLevel = Math.min(100, Math.max(0, level))
  const offset = circumference - (clampedLevel / 100) * circumference
  const isLow = clampedLevel < 15 && !empty

  if (empty) {
    return (
      <div className={clsx('inline-flex items-center gap-1.5', className)}>
        <svg width={size} height={size} className="flex-shrink-0">
          <circle
            cx={size / 2}
            cy={size / 2}
            r={radius}
            fill="none"
            stroke="var(--brand-text-muted)"
            strokeWidth={strokeWidth}
            strokeDasharray="3 3"
            opacity={0.4}
          />
        </svg>
        <span className="text-xs text-[var(--brand-text-muted)]">Empty</span>
      </div>
    )
  }

  return (
    <div className={clsx('inline-flex items-center gap-1.5', className)}>
      <svg width={size} height={size} className="flex-shrink-0" style={{ transform: 'rotate(-90deg)' }}>
        {/* Background track */}
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke="var(--brand-input-bg)"
          strokeWidth={strokeWidth}
        />
        {/* Fill arc */}
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke={color}
          strokeWidth={strokeWidth}
          strokeDasharray={circumference}
          strokeDashoffset={offset}
          strokeLinecap="round"
          style={{ transition: 'stroke-dashoffset 0.5s ease' }}
        />
        {/* Warning ring for low level */}
        {isLow && (
          <circle
            cx={size / 2}
            cy={size / 2}
            r={radius + 2}
            fill="none"
            stroke="var(--status-warning)"
            strokeWidth={1}
            strokeDasharray="2 2"
            opacity={0.6}
          />
        )}
      </svg>
      {material && (
        <span className={clsx(
          'text-xs',
          isLow ? 'text-[var(--status-warning)]' : 'text-[var(--brand-text-secondary)]'
        )}>
          {material}
        </span>
      )}
    </div>
  )
}
