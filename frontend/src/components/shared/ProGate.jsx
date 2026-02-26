import { useLicense } from '../../LicenseContext'
import { useNavigate } from 'react-router-dom'
import { Lock, ExternalLink, ArrowLeft } from 'lucide-react'

export default function ProGate({ feature, children, inline = false, tier = 'Pro' }) {
  const { hasFeature, loading } = useLicense()
  const navigate = useNavigate()

  if (loading) return null
  if (hasFeature(feature)) return children

  if (inline) {
    return (
      <div className="relative">
        <div className="opacity-30 pointer-events-none select-none blur-[1px]">{children}</div>
        <div className="absolute inset-0 flex items-center justify-center">
          <div className="bg-farm-950/90 border border-amber-500/30 rounded-lg px-4 py-3 flex items-center gap-2 shadow-lg">
            <Lock size={14} className="text-amber-400" />
            <span className="text-sm text-farm-300">
              Requires <span className="text-amber-400 font-medium">{tier}</span> license
            </span>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="flex flex-col items-center justify-center min-h-[60vh] text-center px-4">
      <div className="w-16 h-16 rounded-full bg-amber-500/10 border border-amber-500/20 flex items-center justify-center mb-6">
        <Lock size={28} className="text-amber-400" />
      </div>
      <h2 className="text-xl font-semibold text-farm-100 mb-2">{tier} Feature</h2>
      <p className="text-farm-400 max-w-md mb-6">
        This feature requires an O.D.I.N. <span className="text-amber-400 font-medium">{tier}</span> license.
        Upgrade to unlock unlimited printers, multi-user RBAC, orders tracking, analytics, and more.
      </p>
      <div className="flex items-center gap-3">
        <button
          onClick={() => navigate('/')}
          className="inline-flex items-center gap-2 px-5 py-2.5 bg-farm-800 hover:bg-farm-700 text-farm-200 rounded-lg transition-colors"
        >
          <ArrowLeft size={14} /> Back to Dashboard
        </button>
        <a href="https://runsodin.com/#pricing" target="_blank" rel="noopener noreferrer"
          className="inline-flex items-center gap-2 px-5 py-2.5 bg-amber-500 hover:bg-amber-400 text-farm-950 font-semibold rounded-lg transition-colors"
          onClick={(e) => {
            if (!navigator.onLine) {
              e.preventDefault()
              alert('Pricing information is not available offline. Contact your administrator for license details.')
            }
          }}
        >
          View Pricing <ExternalLink size={14} />
        </a>
      </div>
    </div>
  )
}
