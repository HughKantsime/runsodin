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
        <label className="block text-xs font-medium text-[var(--brand-text-secondary)] mb-1">{label}</label>
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
            'w-full bg-[var(--brand-input-bg)] border border-[var(--brand-input-border)] rounded-lg px-3 text-[var(--brand-input-text)] focus:border-[var(--brand-primary)] focus:ring-1 focus:ring-[var(--brand-primary)] focus:outline-none',
            SIZE_CLASSES[size],
            Icon && 'pl-9',
            error && 'border-[var(--status-failed)]',
            className
          )}
          {...rest}
        />
      </div>
      {error && <p className="text-xs text-red-400 mt-1">{error}</p>}
    </div>
  )
})

export default Input
