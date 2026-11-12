import { useState } from 'react'

/** Titles longer than this are shown on two lines until tapped. */
export const LONG_ALERT_TITLE = 80

/**
 * A single alert's title. Short titles render exactly as before; a long one (a full job name)
 * is clamped to two lines and expands in place when pressed, so nothing is lost.
 */
export default function AlertTitle({ title }: { title: string }) {
  const [open, setOpen] = useState(false)
  const style = { color: 'var(--brand-text-primary)' }
  if ((title || '').length <= LONG_ALERT_TITLE) {
    return <div className="text-sm font-semibold" style={style}>{title}</div>
  }
  return (
    <button
      type="button"
      aria-expanded={open}
      title={title}
      onClick={() => setOpen(value => !value)}
      className={`block w-full text-left text-sm font-semibold break-words ${open ? '' : 'line-clamp-2'}`}
      style={style}
    >
      {title}
    </button>
  )
}
