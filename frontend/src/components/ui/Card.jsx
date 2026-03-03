import clsx from 'clsx'

const paddingMap = {
  sm: 'p-3',
  md: 'p-4',
  lg: 'p-5 md:p-6',
}

export default function Card({
  padding = 'md',
  hover = false,
  selected = false,
  className,
  children,
  ...rest
}) {
  return (
    <div
      className={clsx(
        'bg-farm-900 rounded-lg border',
        selected ? 'border-print-500' : 'border-farm-800',
        hover && 'hover:border-farm-700 transition-colors cursor-pointer',
        paddingMap[padding],
        className
      )}
      {...rest}
    >
      {children}
    </div>
  )
}
