import type { ReactNode } from 'react'
import Toast from '../ui/Toast'
import { useFoundry } from '../../hooks/themePreferences'

/** No extra element in Classic. Covers standalone routes as well as the main shell. */
export default function FoundryPage({ name, children }: { name: string; children: ReactNode }) {
  return useFoundry() ? <div data-foundry-page={name}>{children}{['/login', '/setup', '/reset-password'].includes(name) && <Toast />}</div> : <>{children}</>
}
