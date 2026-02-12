import { useQuery } from '@tanstack/react-query'
import { Eye, AlertTriangle } from 'lucide-react'
import { vision } from '../api'
import { useNavigate } from 'react-router-dom'

const TYPE_META = {
  spaghetti: { label: 'Spaghetti', color: 'text-red-400' },
  first_layer: { label: 'First Layer', color: 'text-amber-400' },
  detachment: { label: 'Detachment', color: 'text-orange-400' },
}

function timeAgo(dateStr) {
  if (!dateStr) return ''
  const now = Date.now()
  const d = new Date(dateStr + (dateStr.includes('Z') ? '' : 'Z')).getTime()
  const diff = Math.floor((now - d) / 1000)
  if (diff < 60) return 'just now'
  if (diff < 3600) return Math.floor(diff / 60) + 'm ago'
  if (diff < 86400) return Math.floor(diff / 3600) + 'h ago'
  return Math.floor(diff / 86400) + 'd ago'
}

/**
 * Compact detection feed for embedding in Dashboard or printer detail pages.
 * Props:
 *   printerId - optional, filter to one printer
 *   limit - number of items (default 5)
 */
export default function DetectionFeed({ printerId, limit = 5 }) {
  const navigate = useNavigate()
  const params = { limit, status: 'pending' }
  if (printerId) params.printer_id = printerId

  const { data } = useQuery({
    queryKey: ['vision-detections-feed', printerId],
    queryFn: () => vision.getDetections(params),
    refetchInterval: 15000,
  })

  const items = data?.items || []
  if (items.length === 0) return null

  return (
    <div className="bg-farm-950 rounded-lg border border-farm-800 p-3">
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-sm font-medium flex items-center gap-1.5">
          <Eye size={14} className="text-amber-400" />
          Active Detections
        </h3>
        <button
          onClick={() => navigate('/detections')}
          className="text-xs text-farm-400 hover:text-white transition-colors"
        >
          View all
        </button>
      </div>
      <div className="space-y-1.5">
        {items.map(det => (
          <div
            key={det.id}
            className="flex items-center gap-2 py-1.5 px-2 rounded-lg bg-farm-900 hover:bg-farm-800 cursor-pointer transition-colors"
            onClick={() => navigate('/detections')}
          >
            <AlertTriangle size={12} className={TYPE_META[det.detection_type]?.color || 'text-farm-400'} />
            <span className={`text-xs font-medium ${TYPE_META[det.detection_type]?.color}`}>
              {TYPE_META[det.detection_type]?.label || det.detection_type}
            </span>
            {!printerId && (
              <span className="text-xs text-farm-500 truncate">
                {det.printer_nickname || det.printer_name}
              </span>
            )}
            <span className="text-xs font-mono text-farm-400 ml-auto">
              {(det.confidence * 100).toFixed(0)}%
            </span>
            <span className="text-[10px] text-farm-500">
              {timeAgo(det.created_at)}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}
