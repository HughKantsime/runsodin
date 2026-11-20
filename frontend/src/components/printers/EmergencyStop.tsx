import { useState } from 'react'
import { StopCircle, X, AlertTriangle } from 'lucide-react'
import PageState from '../ui/PageState'
import { isOnline, isPrintingState, isPausedState } from '../../utils/shared'
import { PrintControls, PrintControlsProvider, mayControlPrint, usePrintControls } from './PrintControls'

export default function EmergencyStop() {
  const controls = usePrintControls()
  const [showPanel, setShowPanel] = useState(false)
  if (!controls) return <PrintControlsProvider><EmergencyStop /></PrintControlsProvider>
  if (!mayControlPrint()) return null
  const { printers, listError, outcome } = controls
  const activePrinting = printers.filter(p => isPrintingState(p.gcode_state)).length

  return (
    <>
      {listError && <PageState kind="error" role="alert">Printer status unavailable. Controls are disabled until status refreshes.</PageState>}
      {outcome && <div role="status">{outcome.text}</div>}
      {/* Floating Button */}
      <button
        onClick={() => setShowPanel(!showPanel)}
        className={`emergency-controls-toggle fixed bottom-[calc(3.5rem+env(safe-area-inset-bottom)+0.75rem)] md:bottom-6 right-6 z-40 flex items-center gap-2 px-4 py-3 rounded-md shadow-lg transition-all ${
          showPanel
            ? 'bg-[var(--brand-card-bg)] text-white'
            : 'bg-[var(--brand-content-bg)] border border-red-500/30 text-red-400 hover:bg-red-600 hover:text-white'
        }`}
        aria-label={showPanel ? "Close emergency controls" : `Emergency controls, ${activePrinting} active prints`}
      >
        {showPanel ? (
          <X size={20} />
        ) : (
          <>
            <StopCircle size={20} />
            <span className="font-medium">{activePrinting} Active</span>
          </>
        )}
      </button>

      {/* Control Panel */}
      {showPanel && (
        <div className="emergency-controls-panel fixed bottom-[calc(3.5rem+env(safe-area-inset-bottom)+4rem)] md:bottom-20 right-6 z-40 w-80 bg-[var(--brand-card-bg)] border border-[var(--brand-card-border)] rounded-md shadow-2xl overflow-hidden" role="region" aria-label="Emergency printer controls">
          <div className="p-3 bg-red-900/50 border-b border-red-800 flex items-center gap-2">
            <AlertTriangle className="text-red-400" size={18} aria-hidden="true" />
            <span className="font-medium text-red-200">Emergency Controls</span>
          </div>

          <div className="p-3 space-y-2 max-h-80 overflow-y-auto">
            {!listError && printers.length === 0 && <p>No active prints</p>}
            {printers.map(printer => {
              const isPaused = isPausedState(printer.gcode_state)
              return (
                <div key={printer.id} className="p-3 bg-[var(--brand-card-border)] rounded-md">
                  <div className="flex items-center justify-between mb-2">
                    <span className="font-medium text-sm">{printer.nickname || printer.name}</span>
                    <span className={`text-xs px-2 py-0.5 rounded-md ${
                      isPaused ? 'bg-yellow-900/50 text-yellow-400' : 'bg-green-900/50 text-green-400'
                    }`}>
                      {!isOnline(printer) ? 'Offline' : isPaused ? 'Paused' : isPrintingState(printer.gcode_state) ? 'Printing' : printer.print_stage || 'Idle'}
                    </span>
                  </div>

                  <PrintControls printerId={printer.id} />
                </div>
              )
            })}
          </div>

          <div className="p-2 bg-[var(--brand-card-bg)] text-xs text-[var(--brand-text-muted)] text-center">
            Stop will cancel the print permanently
          </div>
        </div>
      )}
    </>
  )
}
