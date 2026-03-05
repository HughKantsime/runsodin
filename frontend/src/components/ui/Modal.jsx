import { useEffect, useRef, useId } from 'react'
import { X } from 'lucide-react'

const SIZE_CLASSES = {
  sm: 'max-w-sm',
  md: 'max-w-md',
  lg: 'max-w-lg',
  xl: 'max-w-2xl',
}

export default function Modal({
  isOpen,
  onClose,
  title,
  size = 'lg',
  mobileSheet = true,
  children,
  className = '',
  alert = false,
}) {
  const modalRef = useRef(null)
  const titleId = useId()

  // Focus trap and Escape key
  useEffect(() => {
    if (!isOpen) return

    const handleKey = (e) => {
      if (e.key === 'Escape') {
        onClose()
        return
      }
      if (e.key === 'Tab' && modalRef.current) {
        const focusable = modalRef.current.querySelectorAll(
          'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
        )
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
  }, [isOpen, onClose])

  // Body scroll lock
  useEffect(() => {
    if (!isOpen) return
    const original = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = original
    }
  }, [isOpen])

  if (!isOpen) return null

  const alignmentClasses = mobileSheet
    ? 'items-end sm:items-center'
    : 'items-center'

  const roundingClasses = mobileSheet
    ? 'rounded-t-xl sm:rounded-lg'
    : 'rounded-lg'

  return (
    <div
      className={`fixed inset-0 bg-black/60 flex justify-center z-50 p-0 sm:p-4 ${alignmentClasses}`}
      role={alert ? 'alertdialog' : 'dialog'}
      aria-modal="true"
      aria-labelledby={title ? titleId : undefined}
      onClick={onClose}
    >
      <div
        ref={modalRef}
        className={`bg-[var(--brand-card-bg)] w-full ${SIZE_CLASSES[size] || SIZE_CLASSES.lg} max-h-[90vh] overflow-y-auto ${roundingClasses} ${className}`}
        style={{ boxShadow: 'var(--brand-card-shadow)' }}
        onClick={(e) => e.stopPropagation()}
      >
        {title && (
          <div className="flex items-center justify-between p-4 sm:p-6 pb-0">
            <h2
              id={titleId}
              className="text-sm font-semibold text-[var(--brand-text-primary)]"
            >
              {title}
            </h2>
            <button
              onClick={onClose}
              className="text-farm-500 hover:text-farm-300"
              aria-label="Close"
            >
              <X size={20} />
            </button>
          </div>
        )}
        <div className="p-4 sm:p-6">
          {children}
        </div>
      </div>
    </div>
  )
}
