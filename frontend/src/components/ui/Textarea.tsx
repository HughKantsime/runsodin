import { forwardRef, useId, type TextareaHTMLAttributes } from 'react'
import clsx from 'clsx'

interface TextareaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  label?: string
  error?: string
  size?: 'sm' | 'md'
  wrapperClassName?: string
}

const SIZE_CLASSES: Record<string, string> = {
  sm: 'py-1.5 text-xs',
  md: 'py-2 text-sm',
}

const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(function Textarea(
  { label, error, size = 'md', rows = 3, className, wrapperClassName, id, 'aria-describedby': ariaDescribedBy, ...rest },
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
      <textarea
        ref={ref}
        id={controlId}
        aria-invalid={error ? true : undefined}
        aria-describedby={[ariaDescribedBy, error ? errorId : ''].filter(Boolean).join(' ') || undefined}
        rows={rows}
        className={clsx(
          'w-full bg-[var(--brand-input-bg)] border border-[var(--brand-input-border)] rounded-lg px-3 text-[var(--brand-input-text)] focus:border-[var(--brand-primary)] focus:ring-1 focus:ring-[var(--brand-primary)] focus:outline-none',
          SIZE_CLASSES[size],
          error && 'border-[var(--status-failed)]',
          className
        )}
        {...rest}
      />
      {error && <p id={errorId} className="text-xs text-red-400 mt-1">{error}</p>}
    </div>
  )
})

export default Textarea
