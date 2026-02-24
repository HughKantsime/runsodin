import { useState, useEffect } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'

const POLL_INTERVAL = 5000

function formatETA(minutes) {
  if (!minutes || minutes <= 0) return '--'
  const h = Math.floor(minutes / 60)
  const m = Math.round(minutes % 60)
  return h > 0 ? `${h}h ${m}m` : `${m}m`
}

function TempGauge({ label, current, target }) {
  if (current == null) return null
  return (
    <div className="flex items-center justify-between text-sm">
      <span className="text-gray-400">{label}</span>
      <span className="font-mono text-white">
        {Math.round(current)}°C
        {target != null && target > 0 && (
          <span className="text-gray-500"> / {Math.round(target)}°C</span>
        )}
      </span>
    </div>
  )
}

export default function Overlay() {
  const { printerId } = useParams()
  const [searchParams] = useSearchParams()
  const showCamera = searchParams.get('camera') !== 'false'
  const theme = searchParams.get('theme') || 'dark'
  const isDark = theme !== 'light'

  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let active = true

    const poll = async () => {
      try {
        const res = await fetch(`/api/overlay/${printerId}`)
        if (!res.ok) throw new Error(`${res.status}`)
        const json = await res.json()
        if (active) { setData(json); setError(null) }
      } catch (err) {
        if (active) setError(err.message)
      }
    }

    poll()
    const id = setInterval(poll, POLL_INTERVAL)
    return () => { active = false; clearInterval(id) }
  }, [printerId])

  const bg = isDark ? 'bg-black text-white' : 'bg-white text-gray-900'
  const panelBg = isDark ? 'bg-gray-900/80' : 'bg-gray-100'
  const border = isDark ? 'border-gray-800' : 'border-gray-300'
  const mutedText = isDark ? 'text-gray-400' : 'text-gray-500'
  const barBg = isDark ? 'bg-gray-800' : 'bg-gray-300'

  if (error && !data) {
    return (
      <div className={`h-screen flex items-center justify-center ${bg}`}>
        <p className={mutedText}>Printer not found or unavailable</p>
      </div>
    )
  }

  if (!data) {
    return (
      <div className={`h-screen flex items-center justify-center ${bg}`}>
        <p className={mutedText}>Loading...</p>
      </div>
    )
  }

  const progress = data.print_progress ?? 0
  const isPrinting = data.gcode_state === 'RUNNING' || data.gcode_state === 'PAUSE'
  const statusLabel = data.gcode_state === 'RUNNING' ? 'Printing'
    : data.gcode_state === 'PAUSE' ? 'Paused'
    : data.gcode_state === 'FINISH' ? 'Complete'
    : data.gcode_state === 'IDLE' ? 'Idle'
    : data.gcode_state || 'Unknown'

  const statusColor = data.gcode_state === 'RUNNING' ? 'text-green-400'
    : data.gcode_state === 'PAUSE' ? 'text-yellow-400'
    : data.gcode_state === 'FINISH' ? 'text-blue-400'
    : mutedText

  const cameraUrl = data.camera_url
  const showCameraPanel = showCamera && cameraUrl

  return (
    <div className={`h-screen w-screen flex overflow-hidden ${bg}`}>
      {/* Camera panel */}
      {showCameraPanel && (
        <div className="flex-[7] relative bg-black flex items-center justify-center">
          <img
            src={cameraUrl}
            alt="Camera stream"
            className="w-full h-full object-contain"
            onError={(e) => { e.target.style.display = 'none' }}
          />
        </div>
      )}

      {/* Status panel */}
      <div className={`${showCameraPanel ? 'flex-[3]' : 'flex-1 max-w-xl mx-auto'} flex flex-col p-4 gap-4 overflow-y-auto border-l ${border} ${panelBg}`}>
        {/* Printer name + status */}
        <div>
          <h1 className="text-lg font-semibold truncate">{data.printer_name}</h1>
          {data.model && <p className={`text-xs ${mutedText}`}>{data.model}</p>}
          <p className={`text-sm font-medium mt-1 ${statusColor}`}>{statusLabel}</p>
        </div>

        {/* Job info + progress */}
        {isPrinting && (
          <div className={`rounded-lg p-3 ${isDark ? 'bg-gray-800' : 'bg-white'}`}>
            {data.job_name && (
              <p className="text-sm font-medium truncate mb-2">{data.job_name}</p>
            )}

            {/* Progress bar */}
            <div className={`h-3 rounded-full overflow-hidden ${barBg}`}>
              <div
                className="h-full rounded-full bg-green-500 transition-all duration-500"
                style={{ width: `${Math.min(progress, 100)}%` }}
              />
            </div>
            <div className="flex justify-between mt-1 text-xs">
              <span className={mutedText}>{Math.round(progress)}%</span>
              <span className={mutedText}>ETA {formatETA(data.time_remaining_min)}</span>
            </div>

            {/* Layer info */}
            {data.total_layers != null && data.total_layers > 0 && (
              <p className={`text-xs mt-2 ${mutedText}`}>
                Layer {data.current_layer ?? 0} / {data.total_layers}
              </p>
            )}
          </div>
        )}

        {/* Temps */}
        <div className={`rounded-lg p-3 space-y-2 ${isDark ? 'bg-gray-800' : 'bg-white'}`}>
          <TempGauge label="Nozzle" current={data.nozzle_temp} target={data.nozzle_target_temp} />
          <TempGauge label="Bed" current={data.bed_temp} target={data.bed_target_temp} />
        </div>

        {/* Branding watermark */}
        <div className={`mt-auto text-center text-[10px] ${mutedText} select-none`}>
          Powered by O.D.I.N.
        </div>
      </div>
    </div>
  )
}
