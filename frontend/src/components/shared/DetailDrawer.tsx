import { useEffect, useId, useRef, type ReactNode } from 'react'
import { useDialogFocus } from '../ui/useDialogFocus'
import { X } from 'lucide-react'

interface DetailDrawerProps {
  open: boolean
  onClose: () => void
  title: string
  children?: ReactNode
}

export default function DetailDrawer({ open, onClose, title, children }: DetailDrawerProps) {
  const titleId = useId()
  const panelRef = useRef<HTMLDivElement>(null)
  // Focus moves in on open, is trapped, Escape closes, focus returns on close.
  useDialogFocus(panelRef, open, onClose)
  // A closed drawer stays mounted off-screen: inert removes it from the tab
  // order and the accessibility tree (React 18 has no inert prop).
  useEffect(() => {
    const panel = panelRef.current
    if (!panel) return
    if (open) panel.removeAttribute('inert')
    else panel.setAttribute('inert', '')
  }, [open])

  return (
    <>
      {/* Backdrop */}
      <div
        className={`fixed inset-0 bg-black/50 z-50 transition-opacity duration-300 ${open ? 'opacity-100' : 'opacity-0 pointer-events-none'}`}
        aria-hidden="true"
        onClick={onClose}
      />

      {/* Panel */}
      <div
        ref={panelRef}
        role={open ? 'dialog' : undefined}
        aria-modal={open ? true : undefined}
        aria-labelledby={open ? titleId : undefined}
        aria-hidden={open ? undefined : true}
        className={`fixed inset-y-0 right-0 w-full sm:w-[420px] bg-[var(--brand-card-bg)] z-50 flex flex-col transition-transform duration-300 ${open ? 'translate-x-0' : 'translate-x-full'}`}
        style={{ boxShadow: 'var(--brand-card-shadow)' }}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-[var(--brand-card-border)]">
          <h2 id={titleId} className="text-lg font-semibold text-[var(--brand-text-primary)] truncate">{title}</h2>
          <button
            aria-label="Close"
            onClick={onClose}
            className="p-1.5 hover:bg-[var(--brand-card-border)] rounded-md text-[var(--brand-text-secondary)] hover:text-[var(--brand-text-primary)]"
          >
            <X size={18} aria-hidden="true" />
          </button>
        </div>

        {/* Body */}
        <div className="overflow-y-auto flex-1 p-4">
          {children}
        </div>
      </div>
    </>
  )
}
