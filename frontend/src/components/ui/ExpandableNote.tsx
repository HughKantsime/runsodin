import { useId, useState } from 'react'
import { createPortal } from 'react-dom'
import { useTextOverflow } from '../../hooks/useTextOverflow'
import Modal from './Modal'

export default function ExpandableNote({ text }: { text: string }) {
  const id = useId()
  const [expanded, setExpanded] = useState(false)
  const { ref, overflow } = useTextOverflow<HTMLParagraphElement>(text)
  return <div className="text-xs font-normal break-words">
    <p ref={ref} className="whitespace-pre-wrap line-clamp-2">{text}</p>
    {/* The 44px hit area extends upward over the noninteractive preview; only 20px enters row layout. */}
    {overflow && <div className="relative h-5"><button type="button" aria-expanded={expanded} aria-controls={expanded ? id : undefined}
      className="absolute bottom-0 left-0 min-h-11 whitespace-nowrap text-xs underline text-[var(--brand-primary)]"
      style={{ padding: '24px 0 0', lineHeight: '20px' }}
      onClick={event => { event.stopPropagation(); setExpanded(true) }}>
      Show full note
    </button></div>}
    {expanded && createPortal(<Modal isOpen onClose={() => setExpanded(false)} title="Full note" size="xl">
      <p id={id} className="whitespace-pre-wrap break-words text-sm">{text}</p>
    </Modal>, document.body)}
  </div>
}
