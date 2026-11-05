import { Toaster } from 'react-hot-toast'
import { useFoundry } from '../../hooks/themePreferences'

export default function Toast() {
  const foundry = useFoundry()
  return <Toaster position="top-right" toastOptions={{
    ...(foundry ? { className: 'f-toast', iconTheme: { primary: '#dc9a3e', secondary: '#121110' } } : {}),
    style: { background: 'var(--brand-card-bg)', color: 'var(--brand-text-primary)', border: '1px solid var(--brand-card-border)' },
  }} />
}
