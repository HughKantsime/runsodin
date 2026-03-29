import { forwardRef, type ReactNode, type ButtonHTMLAttributes } from 'react'
import { Loader2, type LucideIcon } from 'lucide-react'
import clsx from 'clsx'

type ButtonVariant = 'primary' | 'secondary' | 'tertiary' | 'danger' | 'success' | 'warning' | 'ghost'
type ButtonSize = 'sm' | 'md' | 'lg' | 'icon'

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
  size?: ButtonSize
  icon?: LucideIcon
  iconPosition?: 'left' | 'right'
  loading?: boolean
  disabled?: boolean
  fullWidth?: boolean
  children?: ReactNode
}

const variantClasses: Record<string, string> = {
  primary: 'bg-[var(--brand-primary)] hover:bg-[var(--brand-accent)] text-white',
  secondary: 'border border-[var(--brand-card-border)] bg-transparent hover:bg-[var(--brand-surface)] text-[var(--brand-text-secondary)]',
  ghost: 'text-[var(--brand-text-secondary)] hover:text-[var(--brand-text-primary)] hover:bg-[var(--brand-surface)]',
  danger: 'border border-red-500/30 text-red-400 hover:bg-red-500/10',
}

const variantMap: Record<string, string> = {
  primary: 'primary',
  secondary: 'secondary',
  tertiary: 'secondary',
  danger: 'danger',
  success: 'primary',
  warning: 'primary',
  ghost: 'ghost',
}

const sizeClasses: Record<string, string> = {
  sm: 'px-2.5 py-1 text-xs gap-1.5',
  md: 'px-3.5 py-1.5 text-sm gap-2',
  lg: 'px-5 py-2.5 text-sm gap-2',
  icon: 'p-1.5 min-w-[32px] min-h-[32px] flex items-center justify-center',
}

const spinnerSizes: Record<string, number> = {
  sm: 12,
  md: 14,
  lg: 16,
  icon: 16,
}

const iconSizes: Record<string, number> = {
  sm: 14,
  md: 16,
  lg: 18,
  icon: 16,
}

const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  {
    variant = 'primary',
    size = 'md',
    icon: Icon,
    iconPosition = 'left',
    loading = false,
    disabled = false,
    fullWidth = false,
    children,
    className,
    ...rest
  },
  ref
) {
  const resolvedVariant = variantMap[variant] || 'primary'
  const isDisabled = disabled || loading
  const isIconOnly = size === 'icon'
  const iconPx = iconSizes[size]
  const spinnerPx = spinnerSizes[size]

  const renderIcon = () => {
    if (loading) {
      return <Loader2 size={spinnerPx} className="animate-spin" />
    }
    if (Icon) {
      return <Icon size={iconPx} />
    }
    return null
  }

  return (
    <button
      ref={ref}
      disabled={isDisabled}
      className={clsx(
        'inline-flex items-center justify-center font-medium transition-colors rounded-md',
        'disabled:opacity-50 disabled:cursor-not-allowed',
        variantClasses[resolvedVariant],
        sizeClasses[size],
        fullWidth && 'w-full',
        className
      )}
      {...rest}
    >
      {isIconOnly ? (
        renderIcon() || children
      ) : (
        <>
          {iconPosition === 'left' && renderIcon()}
          {children}
          {iconPosition === 'right' && renderIcon()}
        </>
      )}
    </button>
  )
})

export default Button
