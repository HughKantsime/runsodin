import { useEffect, useRef, useId, type ReactNode } from 'react'
import { useDialogFocus } from './useDialogFocus'
import { X } from 'lucide-react'

type ModalSize = 'sm' | 'md' | 'lg' | 'xl'

interface ModalProps {
  isOpen: boolean
  onClose: () => void
  title?: string
  size?: ModalSize
  mobileSheet?: boolean
  children?: ReactNode
  className?: string
  alert?: boolean
}

const SIZE_CLASSES: Record<string, string> = {
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
}: ModalProps) {
  const modalRef = useRef<HTMLDivElement>(null)
  const titleId = useId()
  const onCloseRef = useRef(onClose)

  // A new callback must not restart focus setup while a form is being edited.
  useEffect(() => {
    onCloseRef.current = onClose
  }, [onClose])

  // Focus in, trap, Escape and focus restore (shared with page-local dialogs).
  useDialogFocus(modalRef, isOpen, () => onCloseRef.current())

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
        tabIndex={-1}
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
              className="text-[var(--brand-text-muted)] hover:text-[var(--brand-text-primary)]"
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
