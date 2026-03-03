import { forwardRef } from 'react'
import clsx from 'clsx'

const SIZE_CLASSES = {
  sm: 'py-1.5 text-xs',
  md: 'py-2 text-sm',
}

const Select = forwardRef(function Select(
  { label, error, options, size = 'md', className, wrapperClassName, children, ...rest },
  ref
) {
  return (
    <div className={wrapperClassName}>
      {label && (
        <label className="block text-sm text-farm-400 mb-1">{label}</label>
      )}
      <select
        ref={ref}
        className={clsx(
          'w-full bg-farm-800 border rounded-lg px-3',
          SIZE_CLASSES[size],
          error ? 'border-red-500' : 'border-farm-700',
          className
        )}
        style={{ color: 'var(--brand-text-primary)' }}
        {...rest}
      >
        {options
          ? options.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))
          : children}
      </select>
      {error && <p className="text-xs text-red-400 mt-1">{error}</p>}
    </div>
  )
})

export default Select
