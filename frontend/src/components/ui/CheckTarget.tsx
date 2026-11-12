import type { ReactElement } from 'react'
import { useFoundry } from '../../hooks/themePreferences'

/** Classic keeps its exact input markup. Foundry uses a native label hit area. */
export default function CheckTarget({ children, className = '', touch = false }: { children: ReactElement; className?: string; touch?: boolean }) {
  const foundry = useFoundry()
  if (foundry) return <label className={`f-check-target ${className}`} onClick={event => event.stopPropagation()}>{children}</label>
  // `touch`: Classic gets a 44 px label hit area around the unchanged visible box (padding offset by negative margin).
  return touch ? <label className="-m-2.5 p-2.5 inline-flex shrink-0 cursor-pointer" onClick={event => event.stopPropagation()}>{children}</label> : children
}
