import type { ReactNode } from 'react'

export default function SlotLabel({ text, children, singleLine = false }: { text: string; children?: ReactNode; singleLine?: boolean }) {
  return <span title={text} className={`slot-label w-full min-w-0 overflow-hidden text-xs text-[var(--brand-text-secondary)] ${singleLine ? 'truncate' : 'line-clamp-2 [overflow-wrap:anywhere]'}`}>{children ?? text}</span>
}
