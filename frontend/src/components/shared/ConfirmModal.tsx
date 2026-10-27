import { useEffect, useRef, type ReactNode } from 'react'
import { AlertTriangle } from 'lucide-react'
import { Modal } from '../ui'

interface ConfirmModalProps {
  open: boolean
  onConfirm: () => void
  onCancel: () => void
  title: string
  message: string
  confirmText?: string
  confirmVariant?: 'danger' | 'success'
  pending?: boolean
  children?: ReactNode
}

export default function ConfirmModal({ open, onConfirm, onCancel, title, message, confirmText = 'Confirm', confirmVariant = 'danger', pending = false, children }: ConfirmModalProps) {
  const confirmRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (open) confirmRef.current?.focus()
  }, [open])

  const btnClass = confirmVariant === 'danger'
    ? 'border border-red-500/60 text-red-400 hover:bg-red-500/10'
    : 'bg-green-600 hover:bg-green-500 text-white'

  return (
    <Modal isOpen={open} onClose={pending ? () => {} : onCancel} size="sm" alert={true} mobileSheet={false}>
      <div className="flex items-center gap-3 mb-4">
        <AlertTriangle size={20} className={confirmVariant === 'danger' ? 'text-red-400' : 'text-amber-400'} />
        <h2 className="text-lg font-semibold text-[var(--brand-text-primary)]">{title}</h2>
      </div>
      <p className="text-sm text-[var(--brand-text-secondary)] mb-6">{message}</p>
      {children}
      <div className="flex items-center justify-end gap-3">
        <button
          disabled={pending}
          onClick={onCancel}
          className="px-4 py-2 text-sm text-[var(--brand-text-secondary)] hover:text-[var(--brand-text-primary)] rounded-md"
        >
          Cancel
        </button>
        <button
          ref={confirmRef}
          disabled={pending}
          onClick={onConfirm}
          className={`px-4 py-2 text-sm rounded-md transition-colors ${btnClass}`}
        >
          {confirmText}
        </button>
      </div>
    </Modal>
  )
}
