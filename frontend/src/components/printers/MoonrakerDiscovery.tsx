import { useState } from 'react'
import { printers } from '../../api'
import type { MoonrakerDiscoveryPrinter } from '../../api'

interface MoonrakerDiscoveryProps {
  onPick: (address: string) => void
}

export default function MoonrakerDiscovery({ onPick }: MoonrakerDiscoveryProps) {
  const [scanning, setScanning] = useState(false)
  const [results, setResults] = useState<MoonrakerDiscoveryPrinter[] | null>(null)
  const [message, setMessage] = useState<string | null>(null)

  const scan = async () => {
    setScanning(true)
    setResults(null)
    setMessage(null)
    try {
      const response = await printers.discoverMoonraker()
      setResults(response.printers)
      setMessage(response.printers.length === 0 ? response.reason || 'No Klipper printers found.' : response.reason)
    } catch {
      setMessage('Discovery failed. Try again in a minute.')
    } finally {
      setScanning(false)
    }
  }

  return (
    <div className="mt-2">
      <button type="button" onClick={scan} disabled={scanning}
        className="px-3 py-1.5 rounded-md border border-[var(--brand-card-border)] text-xs text-[var(--brand-text-secondary)] hover:bg-[var(--brand-card-bg)] disabled:cursor-wait">
        {scanning ? 'Searching the network...' : 'Find Klipper printers on the network'}
      </button>
      {message && <p role="status" className="text-xs text-[var(--brand-text-muted)] mt-2">{message}</p>}
      {results && results.length > 0 && (
        <ul className="mt-2 space-y-1">
          {results.map(found => (
            <li key={`${found.ip}:${found.port}`}>
              <button type="button" disabled={found.already_configured} onClick={() => onPick(found.address)}
                className="w-full text-left px-3 py-2 rounded-md border border-[var(--brand-card-border)] bg-[var(--brand-card-bg)] text-sm disabled:opacity-60">
                <span>{found.name}</span>{' '}
                <span className="font-mono text-xs text-[var(--brand-text-muted)]">{found.address}</span>
                {found.klippy_state && <span className="ml-2 text-xs text-[var(--brand-text-muted)]">Klipper: {found.klippy_state}</span>}
                {found.already_configured && <span className="ml-2 text-xs text-[var(--brand-text-muted)]">Already added</span>}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
