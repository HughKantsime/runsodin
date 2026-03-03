import { forwardRef } from 'react'
import clsx from 'clsx'

const SIZE_CLASSES = {
  sm: 'py-1.5 text-xs',
  md: 'py-2 text-sm',
}

const Textarea = forwardRef(function Textarea(
  { label, error, size = 'md', rows = 3, className, wrapperClassName, ...rest },
  ref
) {
  return (
    <div className={wrapperClassName}>
      {label && (
        <label className="block text-sm text-farm-400 mb-1">{label}</label>
      )}
      <textarea
        ref={ref}
        rows={rows}
        className={clsx(
          'w-full bg-farm-800 border rounded-lg px-3',
          SIZE_CLASSES[size],
          error ? 'border-red-500' : 'border-farm-700',
          className
        )}
        style={{ color: 'var(--brand-text-primary)' }}
        {...rest}
      />
      {error && <p className="text-xs text-red-400 mt-1">{error}</p>}
    </div>
  )
})

export default Textarea
