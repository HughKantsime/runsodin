import clsx from 'clsx'

const colorMap = {
  green: 'bg-green-500',
  red: 'bg-red-500',
  yellow: 'bg-yellow-500',
  blue: 'bg-blue-500',
  print: 'bg-print-500',
}

const sizeMap = {
  sm: 'h-1.5',
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
        'w-full bg-farm-700 rounded-full',
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
          colorMap[color] || colorMap.print
        )}
        style={{ width: `${clamped}%` }}
      />
    </div>
  )
}
