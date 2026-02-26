import { useEffect, useRef } from 'react'
import { AlertTriangle } from 'lucide-react'

export default function ConfirmModal({ open, onConfirm, onCancel, title, message, confirmText = 'Confirm', confirmVariant = 'danger' }) {
  const confirmRef = useRef(null)
  const modalRef = useRef(null)

  useEffect(() => {
    if (!open) return
    confirmRef.current?.focus()
    const handleKey = (e) => {
      if (e.key === 'Escape') onCancel()
      if (e.key === 'Tab' && modalRef.current) {
        const focusable = modalRef.current.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')
        if (focusable.length === 0) return
        const first = focusable[0]
        const last = focusable[focusable.length - 1]
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault()
          last.focus()
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault()
          first.focus()
        }
      }
    }
    document.addEventListener('keydown', handleKey)
    return () => document.removeEventListener('keydown', handleKey)
  }, [open, onCancel])

  if (!open) return null

  const btnClass = confirmVariant === 'danger'
    ? 'bg-red-600 hover:bg-red-500 text-white'
    : 'bg-green-600 hover:bg-green-500 text-white'

  return (
    <div
      className="fixed inset-0 bg-black/50 flex items-center justify-center z-50"
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="confirm-modal-title"
      aria-describedby="confirm-modal-message"
      onClick={onCancel}
    >
      <div
        ref={modalRef}
        className="bg-farm-950 border border-farm-700 rounded-lg shadow-2xl w-full max-w-sm mx-4 p-6"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center gap-3 mb-4">
          <AlertTriangle size={20} className={confirmVariant === 'danger' ? 'text-red-400' : 'text-amber-400'} />
          <h2 id="confirm-modal-title" className="text-lg font-semibold text-farm-100">{title}</h2>
        </div>
        <p id="confirm-modal-message" className="text-sm text-farm-300 mb-6">{message}</p>
        <div className="flex items-center justify-end gap-3">
          <button
            onClick={onCancel}
            className="px-4 py-2 text-sm text-farm-400 hover:text-farm-200 rounded-md"
          >
            Cancel
          </button>
          <button
            ref={confirmRef}
            onClick={onConfirm}
            className={`px-4 py-2 text-sm rounded-md transition-colors ${btnClass}`}
          >
            {confirmText}
          </button>
        </div>
      </div>
    </div>
  )
}
