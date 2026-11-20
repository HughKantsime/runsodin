interface PrintProgressProps {
  age?: string
  percent: unknown
  remainingMinutes: unknown
}

export default function PrintProgress({ percent, remainingMinutes, age = '' }: PrintProgressProps) {
  const progress = typeof percent === 'number' && Number.isFinite(percent) && percent >= 0 && percent <= 100 ? percent : null
  const minutes = typeof remainingMinutes === 'number' && Number.isFinite(remainingMinutes) && remainingMinutes >= 0 ? Math.round(remainingMinutes) : null
  const remaining = minutes === null ? 'Time remaining unknown' : minutes < 60 ? `${minutes}m left` : `${Math.floor(minutes / 60)}h ${minutes % 60}m left`
  const tone = age ? 'text-[var(--brand-text-muted)]' : 'text-[var(--brand-text-secondary)]'
  return <div>
    <div className={`printer-number flex flex-wrap items-center justify-between gap-x-3 gap-y-1 text-xs mb-2 ${tone} [overflow-wrap:anywhere]`}>
      <span>{progress === null ? 'Progress unknown' : `${progress}%${age}`}</span>
      <span className={tone}>{remaining}{minutes !== null && age}</span>
    </div>
    {progress !== null && <div role="progressbar" aria-label={age ? `Print progress, ${progress}%${age}` : "Print progress"} aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress} aria-valuetext={age ? `${progress}%${age}` : undefined} className="w-full bg-[var(--brand-card-border)] rounded-full h-2 overflow-hidden">
      <div className="bg-green-500 h-2 rounded-full" style={{ width: `${progress}%` }} />
    </div>}
  </div>
}
