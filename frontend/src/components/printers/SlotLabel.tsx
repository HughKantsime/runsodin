import type { ReactNode } from 'react'

export default function SlotLabel({ text, children }: { text: string; children?: ReactNode }) {
  return <span title={text} className="slot-label w-full min-w-0 line-clamp-2 overflow-hidden [overflow-wrap:anywhere] text-xs text-[var(--brand-text-secondary)]">{children ?? text}</span>
}
