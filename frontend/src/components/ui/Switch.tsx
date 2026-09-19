import { forwardRef, type InputHTMLAttributes } from 'react'
import clsx from 'clsx'

interface SwitchProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'type'> {
  label: string
  description?: string
}

const Switch = forwardRef<HTMLInputElement, SwitchProps>(function Switch(
  { label, description, checked, disabled, className, ...props },
  ref,
) {
  return (
    <label className={clsx('flex items-center justify-between gap-4', disabled && 'opacity-50', className)}>
      <span>
        <span className="block text-sm font-medium text-[var(--brand-text-primary)]">{label}</span>
        {description && <span className="mt-0.5 block text-xs text-[var(--brand-text-secondary)]">{description}</span>}
      </span>
      <span className="relative inline-flex shrink-0">
        <input ref={ref} type="checkbox" checked={checked} disabled={disabled} className="peer sr-only" {...props} />
        <span className="h-6 w-11 rounded-full border border-[var(--brand-input-border)] bg-[var(--brand-input-bg)] transition-colors peer-checked:border-[var(--brand-primary)] peer-checked:bg-[var(--brand-primary)] peer-focus-visible:ring-2 peer-focus-visible:ring-[var(--brand-primary)] peer-focus-visible:ring-offset-2 peer-focus-visible:ring-offset-[var(--brand-card-bg)]" />
        <span className="pointer-events-none absolute left-1 top-1 h-4 w-4 rounded-full bg-[var(--brand-text-secondary)] transition-transform peer-checked:translate-x-5 peer-checked:bg-[var(--brand-on-primary)]" />
      </span>
    </label>
  )
})

export default Switch
