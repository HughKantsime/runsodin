import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { timelapses, type TimelapseCaptureStatus } from '../../api/cameras'

function ago(iso: string | null | undefined, nowMs: number): string | null {
  if (!iso) return null
  const then = Date.parse(iso)
  if (Number.isNaN(then)) return null
  const seconds = Math.max(0, Math.round((nowMs - then) / 1000))
  if (seconds < 60) return `${seconds} s ago`
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`
  return `${Math.floor(seconds / 3600)} h ago`
}

/** Returns null for anything that is not a capture-status block, so no line is shown. */
export function describeTimelapseStatus(status: unknown, nowMs: number): string | null {
  const s = status as Partial<TimelapseCaptureStatus> | null | undefined
  if (!s || typeof s.enabled !== 'boolean' || typeof s.capturing !== 'boolean') return null
  if (!s.enabled) return 'Timelapse off'
  if (s.capturing) {
    const frames = typeof s.frames_captured === 'number' ? s.frames_captured : 0
    const last = ago(s.last_frame_at, nowMs)
    const base = `Recording timelapse: ${frames} ${frames === 1 ? 'frame' : 'frames'}, ${last ? `last frame ${last}` : 'no frame saved yet'}`
    return s.last_error ? `${base}. ${s.last_error}` : base
  }
  return s.last_error ? `Not recording: ${s.last_error}` : 'Timelapse on, waiting for a print'
}

export default function TimelapseStatusLine({ printerId }: { printerId: number }) {
  const { data } = useQuery({
    queryKey: ['timelapse-status', printerId],
    queryFn: ({ signal }) => timelapses.captureStatus(printerId, signal),
    refetchInterval: 10000,
    retry: false,
  })
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 5000)
    return () => clearInterval(timer)
  }, [])
  // A refused or failed request (for example controlled work) shows no line.
  const text = describeTimelapseStatus(data, now)
  if (!text) return null
  return <p className="mt-2 text-xs text-[var(--brand-text-secondary)]" data-testid="timelapse-status">{text}</p>
}
