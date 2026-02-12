export default function ProBadge({ className = '' }) {
  return (
    <span className={`inline-flex items-center px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider bg-amber-500/20 text-amber-400 border border-amber-500/30 rounded-lg ml-auto ${className}`}>
      PRO
    </span>
  )
}
