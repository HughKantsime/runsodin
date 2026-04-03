interface ProBadgeProps {
  className?: string
}

export default function ProBadge({ className = '' }: ProBadgeProps) {
  return (
    <span
      className={`inline-flex items-center text-xs text-[var(--brand-primary)] font-medium uppercase tracking-wider ml-auto ${className}`}
      title="Requires a Pro, Education, or Enterprise license"
    >
      PRO
    </span>
  )
}
