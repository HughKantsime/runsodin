import clsx from 'clsx'

export default function TabBar({
  tabs,
  active,
  onChange,
  variant = 'pill',
}) {
  const isPill = variant === 'pill'

  return (
    <div
      className={clsx(
        'inline-flex',
        isPill
          ? 'bg-farm-900 p-1 rounded-lg border border-farm-800'
          : 'bg-farm-800 rounded-lg p-0.5'
      )}
    >
      {tabs.map((tab) => {
        const isActive = tab.value === active
        const Icon = tab.icon

        return (
          <button
            key={tab.value}
            onClick={() => onChange(tab.value)}
            className={clsx(
              'flex items-center gap-2 px-3 py-1.5 text-sm rounded-md font-medium transition-colors',
              isActive
                ? 'bg-print-600 text-white'
                : isPill
                  ? 'text-farm-400 hover:text-farm-200 hover:bg-farm-800'
                  : 'text-farm-400 hover:text-farm-200'
            )}
          >
            {Icon && <Icon size={14} />}
            <span>{tab.label}</span>
            {tab.count != null && (
              <span
                className={clsx(
                  'text-xs opacity-60 ml-1',
                  isActive ? 'opacity-80' : ''
                )}
              >
                {tab.count}
              </span>
            )}
          </button>
        )
      })}
    </div>
  )
}
