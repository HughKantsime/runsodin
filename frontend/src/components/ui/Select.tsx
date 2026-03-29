import { forwardRef, type ReactNode, type SelectHTMLAttributes } from 'react'
import clsx from 'clsx'

interface SelectOption {
  value: string | number
  label: string
}

interface SelectProps extends SelectHTMLAttributes<HTMLSelectElement> {
  label?: string
  error?: string
  options?: SelectOption[]
  size?: 'sm' | 'md'
  wrapperClassName?: string
  children?: ReactNode
}

const SIZE_CLASSES: Record<string, string> = {
  sm: 'py-1.5 text-xs',
  md: 'py-2 text-sm',
}

const Select = forwardRef<HTMLSelectElement, SelectProps>(function Select(
  { label, error, options, size = 'md', className, wrapperClassName, children, ...rest },
  ref
) {
  return (
    <div className={wrapperClassName}>
      {label && (
        <label className="block text-xs font-medium text-[var(--brand-text-secondary)] mb-1">{label}</label>
      )}
      <select
        ref={ref}
        className={clsx(
          'w-full bg-[var(--brand-input-bg)] border border-[var(--brand-input-border)] rounded-lg px-3 text-[var(--brand-input-text)] focus:border-[var(--brand-primary)] focus:ring-1 focus:ring-[var(--brand-primary)] focus:outline-none',
          SIZE_CLASSES[size],
          error && 'border-[var(--status-failed)]',
          className
        )}
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
