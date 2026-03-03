import { useEffect, useRef } from 'react'
import { AlertTriangle } from 'lucide-react'
import { Modal } from '../ui'

export default function ConfirmModal({ open, onConfirm, onCancel, title, message, confirmText = 'Confirm', confirmVariant = 'danger' }) {
  const confirmRef = useRef(null)

  useEffect(() => {
    if (open) confirmRef.current?.focus()
  }, [open])

  const btnClass = confirmVariant === 'danger'
    ? 'bg-red-600 hover:bg-red-500 text-white'
    : 'bg-green-600 hover:bg-green-500 text-white'

  return (
    <Modal isOpen={open} onClose={onCancel} size="sm" alert={true} mobileSheet={false}>
      <div className="flex items-center gap-3 mb-4">
        <AlertTriangle size={20} className={confirmVariant === 'danger' ? 'text-red-400' : 'text-amber-400'} />
        <h2 className="text-lg font-semibold text-farm-100">{title}</h2>
      </div>
      <p className="text-sm text-farm-300 mb-6">{message}</p>
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
    </Modal>
  )
}
