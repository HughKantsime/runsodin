import { useId, useState } from 'react'

export default function ExpandableNote({ text }: { text: string }) {
  const id = useId()
  const [expanded, setExpanded] = useState(false)
  return <div className="text-xs font-normal break-words">
    <p id={id} className={`whitespace-pre-wrap ${expanded ? '' : 'line-clamp-2'}`}>{text}</p>
    <button type="button" aria-expanded={expanded} aria-controls={id}
      className="min-h-11 underline text-[var(--brand-primary)]"
      onClick={event => { event.stopPropagation(); setExpanded(!expanded) }}>
      {expanded ? 'Show less' : 'Show full note'}
    </button>
  </div>
}
