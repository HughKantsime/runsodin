import { forwardRef, useId, type InputHTMLAttributes } from 'react'
import { type LucideIcon } from 'lucide-react'
import clsx from 'clsx'

interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  label?: string
  error?: string
  icon?: LucideIcon
  size?: 'sm' | 'md'
  wrapperClassName?: string
}

const SIZE_CLASSES: Record<string, string> = {
  sm: 'py-1.5 text-xs',
  md: 'py-2 text-sm',
}

const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { label, error, icon: Icon, size = 'md', className, wrapperClassName, id, 'aria-describedby': ariaDescribedBy, ...rest },
  ref
) {
  const generatedId = useId()
  const controlId = id || generatedId
  const errorId = `${controlId}-error`
  return (
    <div className={wrapperClassName}>
      {label && (
        <label htmlFor={controlId} className="block text-xs font-medium text-[var(--brand-text-secondary)] mb-1">{label}</label>
      )}
      <div className="relative">
        {Icon && (
          <Icon
            size={size === 'sm' ? 14 : 16}
            className="absolute left-3 top-1/2 -translate-y-1/2 text-[var(--brand-text-muted)] pointer-events-none"
          />
        )}
        <input
          ref={ref}
          id={controlId}
          aria-invalid={error ? true : undefined}
          aria-describedby={[ariaDescribedBy, error ? errorId : ''].filter(Boolean).join(' ') || undefined}
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
      {error && <p id={errorId} className="text-xs text-red-400 mt-1">{error}</p>}
    </div>
  )
})

export default Input
