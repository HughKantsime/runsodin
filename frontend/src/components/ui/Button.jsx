import { forwardRef } from 'react'
import { Loader2 } from 'lucide-react'
import clsx from 'clsx'

const variantClasses = {
  primary: 'bg-print-600 hover:bg-print-500 text-white',
  secondary: 'bg-farm-800 hover:bg-farm-700 text-farm-300',
  tertiary: 'bg-farm-700 hover:bg-farm-600 text-farm-300',
  danger: 'bg-red-600 hover:bg-red-500 text-white',
  success: 'bg-green-600 hover:bg-green-500 text-white',
  warning: 'bg-amber-600 hover:bg-amber-500 text-white',
  ghost: 'text-farm-400 hover:text-farm-200 hover:bg-farm-800',
}

const sizeClasses = {
  sm: 'px-2 md:px-3 py-1 text-xs rounded-md gap-1.5',
  md: 'px-4 py-2 text-sm rounded-lg gap-2',
  lg: 'px-6 py-3 text-base rounded-lg gap-2',
  icon: 'p-1.5 md:p-2 rounded-lg',
}

const spinnerSizes = {
  sm: 12,
  md: 14,
  lg: 16,
  icon: 16,
}

const iconSizes = {
  sm: 14,
  md: 16,
  lg: 18,
  icon: 16,
}

const Button = forwardRef(function Button(
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
        'inline-flex items-center justify-center font-medium transition-colors',
        'disabled:opacity-50 disabled:cursor-not-allowed',
        variantClasses[variant],
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
