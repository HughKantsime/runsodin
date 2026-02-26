import { useState } from 'react'
import { X, ExternalLink } from 'lucide-react'
import { useLicense } from '../../LicenseContext'

export default function UpgradeBanner() {
  const { tier, loading, isPro } = useLicense()
  const [dismissed, setDismissed] = useState(false)

  if (loading || isPro || dismissed) return null

  return (
    <div className="flex items-center justify-between gap-3 px-4 py-2 bg-amber-900/30 border-b border-amber-700/30 text-sm">
      <p className="text-amber-200/90">
        <span className="font-medium text-amber-300">Community Edition</span>
        {' '}&mdash; 5 printers, 1 user.{' '}
        <a
          href="https://runsodin.com/pricing"
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1 text-amber-400 hover:text-amber-300 underline underline-offset-2"
        >
          Upgrade to Pro for unlimited <ExternalLink size={12} />
        </a>
      </p>
      <button
        onClick={() => setDismissed(true)}
        className="p-1 text-amber-400/60 hover:text-amber-300 transition-colors flex-shrink-0"
        aria-label="Dismiss upgrade banner"
      >
        <X size={14} />
      </button>
    </div>
  )
}
