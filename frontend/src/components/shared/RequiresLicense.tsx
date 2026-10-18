import { Lock } from 'lucide-react'

// Inline upgrade notice for a Pro-only section inside a tab that is otherwise free.
export default function RequiresLicense({ tier = 'Pro', what }: { tier?: string; what: string }) {
  return (
    <p className="flex items-center gap-2 text-sm text-[var(--brand-text-secondary)]">
      <Lock size={14} className="text-[var(--brand-primary)]" />
      <span>{what} requires an O.D.I.N. <span className="text-[var(--brand-primary)] font-medium">{tier}</span> license.</span>
    </p>
  )
}
