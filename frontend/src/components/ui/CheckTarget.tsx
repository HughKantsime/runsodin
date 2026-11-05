import type { ReactElement } from 'react'
import { useFoundry } from '../../hooks/themePreferences'

/** Classic keeps its exact input markup. Foundry uses a native label hit area. */
export default function CheckTarget({ children, className = '' }: { children: ReactElement; className?: string }) {
  const foundry = useFoundry()
  return foundry ? <label className={`f-check-target ${className}`} onClick={event => event.stopPropagation()}>{children}</label> : children
}
