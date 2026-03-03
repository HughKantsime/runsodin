import { ExternalLink } from 'lucide-react'
import { Modal } from '../ui'

export default function UpgradeModal({ isOpen, onClose, resource = 'printers' }) {
  const label = resource === 'users' ? 'users' : 'printers'

  return (
    <Modal isOpen={isOpen} onClose={onClose} title="Limit Reached" size="sm" mobileSheet={false}>
      <p className="text-sm text-farm-400 mb-6">
        You've reached the Community tier limit for {label}. Upgrade to Pro for unlimited {label}, plus multi-user RBAC, analytics, and more.
      </p>
      <div className="flex items-center gap-3">
        <button
          onClick={onClose}
          className="flex-1 px-4 py-2.5 bg-farm-800 hover:bg-farm-700 text-farm-300 rounded-lg text-sm transition-colors"
        >
          Close
        </button>
        <a
          href="https://runsodin.com/pricing"
          target="_blank"
          rel="noopener noreferrer"
          className="flex-1 inline-flex items-center justify-center gap-2 px-4 py-2.5 bg-amber-500 hover:bg-amber-400 text-farm-950 font-semibold rounded-lg text-sm transition-colors"
        >
          View Pricing <ExternalLink size={14} />
        </a>
      </div>
    </Modal>
  )
}
