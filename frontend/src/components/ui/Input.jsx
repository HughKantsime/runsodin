import { forwardRef } from 'react'
import clsx from 'clsx'

const SIZE_CLASSES = {
  sm: 'py-1.5 text-xs',
  md: 'py-2 text-sm',
}

const Input = forwardRef(function Input(
  { label, error, icon: Icon, size = 'md', className, wrapperClassName, ...rest },
  ref
) {
  return (
    <div className={wrapperClassName}>
      {label && (
        <label className="block text-sm text-farm-400 mb-1">{label}</label>
      )}
      <div className="relative">
        {Icon && (
          <Icon
            size={size === 'sm' ? 14 : 16}
            className="absolute left-3 top-1/2 -translate-y-1/2 text-farm-500 pointer-events-none"
          />
        )}
        <input
          ref={ref}
          className={clsx(
            'w-full bg-farm-800 border rounded-lg px-3',
            SIZE_CLASSES[size],
            Icon && 'pl-9',
            error ? 'border-red-500' : 'border-farm-700',
            className
          )}
          style={{ color: 'var(--brand-text-primary)' }}
          {...rest}
        />
      </div>
      {error && <p className="text-xs text-red-400 mt-1">{error}</p>}
    </div>
  )
})

export default Input
