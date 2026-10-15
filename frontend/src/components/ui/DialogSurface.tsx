import { useRef, type HTMLAttributes } from 'react'
import { useDialogFocus } from './useDialogFocus'

interface DialogSurfaceProps extends HTMLAttributes<HTMLDivElement> {
  /** Called on Escape. */
  onEscape?: () => void
}

/**
 * A role=dialog element rendered only while its dialog is open: focus moves in on
 * mount, is trapped while mounted, and returns to the opener on unmount.
 */
export default function DialogSurface({ onEscape, children, ...rest }: DialogSurfaceProps) {
  const ref = useRef<HTMLDivElement>(null)
  useDialogFocus(ref, true, onEscape)
  return <div ref={ref} {...rest}>{children}</div>
}
