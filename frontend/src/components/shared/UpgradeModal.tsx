import { ExternalLink } from 'lucide-react'
import { Modal } from '../ui'

interface UpgradeModalProps {
  isOpen: boolean
  onClose: () => void
  resource?: 'printers' | 'users'
}

export default function UpgradeModal({ isOpen, onClose, resource = 'printers' }: UpgradeModalProps) {
  const label = resource === 'users' ? 'users' : 'printers'

  return (
    <Modal isOpen={isOpen} onClose={onClose} title="Limit Reached" size="sm" mobileSheet={false}>
      <p className="text-sm text-[var(--brand-text-secondary)] mb-6">
        You've reached the Community tier limit for {label}. Upgrade to Pro for unlimited {label}, plus multi-user RBAC, analytics, and more.
      </p>
      <div className="flex items-center gap-3">
        <button
          onClick={onClose}
          className="flex-1 px-4 py-2.5 bg-[var(--brand-card-bg)] hover:bg-[var(--brand-card-border)] text-[var(--brand-text-secondary)] rounded-md text-sm transition-colors"
        >
          Close
        </button>
        <a
          href="https://runsodin.com/pricing"
          target="_blank"
          rel="noopener noreferrer"
          className="flex-1 inline-flex items-center justify-center gap-2 px-4 py-2.5 border border-[var(--brand-card-border)] hover:bg-[var(--brand-card-border)] text-[var(--brand-text-primary)] font-medium rounded-md text-sm transition-colors"
        >
          View Pricing <ExternalLink size={14} />
        </a>
      </div>
    </Modal>
  )
}
