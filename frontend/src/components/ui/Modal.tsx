import { useFoundry } from '../../hooks/themePreferences'
import { useEffect, useRef, useId, type ReactNode } from 'react'
import { useDialogFocus } from './useDialogFocus'
import { X } from 'lucide-react'
import { useMediaQuery } from '../../hooks/useMediaQuery'

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
  const foundry = useFoundry()
  // Under 640 px a (non-alert) modal is a full-screen sheet. Same breakpoint as the slice-1/2 phone cards.
  const phone = useMediaQuery('(max-width: 639px)')
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

  const sheet = phone && !alert

  const alignmentClasses = mobileSheet
    ? 'items-end sm:items-center'
    : 'items-center'

  const roundingClasses = mobileSheet
    ? 'rounded-t-xl sm:rounded-lg'
    : 'rounded-lg'

  return (
    <div
      className={`fixed inset-0 bg-black/60 flex justify-center z-50 p-0 sm:p-4 ${sheet ? 'items-stretch' : alignmentClasses}`}
      role={alert ? 'alertdialog' : 'dialog'}
      aria-modal="true"
      aria-labelledby={title ? titleId : undefined}
      onClick={onClose}
    >
      <div
        data-foundry-ui={foundry ? 'modal' : undefined}
        ref={modalRef}
        tabIndex={-1}
        className={`bg-[var(--brand-card-bg)] w-full ${sheet ? 'h-full max-w-none flex flex-col overflow-hidden' : `${SIZE_CLASSES[size] || SIZE_CLASSES.lg} max-h-[90vh] overflow-y-auto ${roundingClasses}`} ${className}`}
        style={{ boxShadow: 'var(--brand-card-shadow)' }}
        onClick={(e) => e.stopPropagation()}
      >
        {(title || sheet) && (
          <div className={sheet ? 'flex items-center justify-between p-4 pb-2 shrink-0 border-b border-[var(--brand-card-border)]' : 'flex items-center justify-between p-4 sm:p-6 pb-0'}>
            {title ? (
              <h2
                id={titleId}
                className="text-sm font-semibold text-[var(--brand-text-primary)]"
              >
                {title}
              </h2>
            ) : <span />}
            <button
              onClick={onClose}
              className={sheet
                ? 'text-[var(--brand-text-muted)] hover:text-[var(--brand-text-primary)] -mr-2 min-h-11 min-w-11 flex items-center justify-center'
                : 'text-[var(--brand-text-muted)] hover:text-[var(--brand-text-primary)]'}
              aria-label="Close"
            >
              <X size={20} />
            </button>
          </div>
        )}
        <div className={sheet
          ? 'flex-1 min-h-0 overflow-y-auto p-4 pb-[calc(1rem+env(safe-area-inset-bottom))] [&_input]:text-base [&_select]:text-base [&_textarea]:text-base [&_[data-modal-footer]]:sticky [&_[data-modal-footer]]:bottom-0 [&_[data-modal-footer]]:z-10 [&_[data-modal-footer]]:bg-[var(--brand-card-bg)] [&_[data-modal-footer]]:pb-3'
          : 'p-4 sm:p-6'}>
          {children}
        </div>
      </div>
    </div>
  )
}
