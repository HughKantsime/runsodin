import clsx from 'clsx'

const colorMap = {
  green: { bg: 'bg-green-400/10', text: 'text-green-400' },
  blue: { bg: 'bg-blue-400/10', text: 'text-blue-400' },
  amber: { bg: 'bg-amber-400/10', text: 'text-amber-400' },
  red: { bg: 'bg-red-400/10', text: 'text-red-400' },
  purple: { bg: 'bg-purple-400/10', text: 'text-purple-400' },
  default: { bg: 'bg-farm-800/50', text: 'text-farm-400' },
}

export default function StatCard({
  label,
  value,
  icon: Icon,
  color = 'default',
  subtitle,
  onClick,
  className,
}) {
  const colors = colorMap[color] || colorMap.default

  return (
    <div
      className={clsx(
        'bg-farm-900 rounded-lg border border-farm-800 p-3 md:p-4',
        onClick && 'cursor-pointer hover:border-farm-700 transition-colors',
        className
      )}
      onClick={onClick}
      role={onClick ? 'button' : undefined}
    >
      <div className="flex items-start justify-between">
        <div>
          <p className="text-farm-500 text-sm">{label}</p>
          <p
            className="text-2xl font-display font-bold"
            style={{ color: 'var(--brand-text-primary)' }}
          >
            {value}
          </p>
          {subtitle && <p className="text-xs text-farm-500 mt-1">{subtitle}</p>}
        </div>
        {Icon && (
          <div className={clsx('p-2 rounded-lg', colors.bg)}>
            <Icon size={20} className={colors.text} />
          </div>
        )}
      </div>
    </div>
  )
}
