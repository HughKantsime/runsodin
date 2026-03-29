import { type ReactNode, type HTMLAttributes } from 'react'
import clsx from 'clsx'

interface CardProps extends HTMLAttributes<HTMLDivElement> {
  children?: ReactNode
  padding?: 'sm' | 'md' | 'lg'
  selected?: boolean
}

export default function Card({ children, className, padding = 'md', selected, onClick, ...props }: CardProps) {
  const paddingClasses: Record<string, string> = {
    sm: 'p-3',
    md: 'p-4',
    lg: 'p-5',
  }

  return (
    <div
      className={clsx(
        'rounded-md transition-colors',
        'bg-[var(--brand-card-bg)]',
        selected && 'ring-1 ring-[var(--brand-primary)]',
        onClick && 'cursor-pointer hover:brightness-110',
        paddingClasses[padding],
        className
      )}
      style={{ boxShadow: 'var(--brand-card-shadow)' }}
      onClick={onClick}
      {...props}
    >
      {children}
    </div>
  )
}
