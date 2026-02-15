import { ExternalLink, X } from 'lucide-react'

export default function UpgradeModal({ isOpen, onClose, resource = 'printers' }) {
  if (!isOpen) return null

  const label = resource === 'users' ? 'users' : 'printers'

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60" onClick={onClose}>
      <div
        className="bg-farm-900 border border-farm-700 rounded-xl shadow-2xl max-w-sm w-full p-6"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between mb-4">
          <h3 className="text-lg font-display font-semibold text-farm-100">
            Limit Reached
          </h3>
          <button onClick={onClose} className="text-farm-500 hover:text-farm-300 transition-colors">
            <X size={18} />
          </button>
        </div>
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
      </div>
    </div>
  )
}
