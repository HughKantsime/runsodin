import { useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import useWebRTC from '../../hooks/useWebRTC'
import useCameraList from '../../hooks/useCameraList'
import CameraFallback, { type FallbackState } from '../printers/CameraFallback'
import { isPausedState } from '../../utils/shared'
import type { Camera, Printer, PrintJob } from '../../types'
import { activePrint, number, useFloorData } from './data'

export function CameraFeed({ camera, printer, job }: { camera: Camera; printer?: Printer; job?: PrintJob }) {
  const { videoRef, status, fallback, retry } = useWebRTC(camera.id)
  const [stream, setStream] = useState<FallbackState>({ transport: 'mjpeg', live: false, failed: false })
  const live = fallback ? stream.live : status === 'live'
  const transport = fallback ? (stream.transport === 'mjpeg' ? 'MJPEG' : 'Snapshots') : 'WebRTC'
  const failed = fallback ? stream.failed : ['error', 'disconnected'].includes(status)
  return <div className="f-camera-feed" aria-label={`${camera.name || 'Printer'} camera`}>
    <video ref={videoRef} autoPlay playsInline muted />
    <CameraFallback cameraId={camera.id} active={!!fallback} onState={setStream} hideUnavailable />
    <div className="f-camera-overlay f-mono"><span>{live ? 'LIVE' : failed ? 'Unavailable' : fallback && stream.transport === 'snapshot' ? 'Snapshot' : 'Connecting'} · {camera.name} · {transport}</span>
      <span>{[number(job?.progress_percent) ? `${job.progress_percent}%` : '', number(job?.current_layer) && number(job?.total_layers) ? `L ${job.current_layer}/${job.total_layers}` : '', number(printer?.nozzle_temp) ? `Nozzle ${Math.round(printer.nozzle_temp)}°` : ''].filter(Boolean).join(' · ')}</span>
    </div>
    {failed && <div className="f-camera-empty"><p role="status">Camera unavailable. Check the camera connection.</p><button className="f-button" type="button" onClick={retry}>Retry camera</button></div>}
  </div>
}
export default function CameraView({ cameras, printers = [], prints = [], initialId }: { cameras: Camera[]; printers?: Printer[]; prints?: PrintJob[]; initialId?: number }) {
  const [selection, select] = useState<number | 'wall'>(initialId || cameras[0]?.id)
  const root = useRef<HTMLElement>(null)
  const tabs = useRef<HTMLDivElement>(null)
  const [error, setError] = useState('')
  const selected = selection === 'wall' ? 'wall' : cameras.some(c => c.id === selection) ? selection : cameras[0]?.id
  // On a narrow screen the chooser scrolls sideways: keep the chosen camera's button in view.
  // Moves the strip only, never the page.
  useEffect(() => {
    const keepVisible = () => {
      const strip = tabs.current
      const active = strip?.querySelector<HTMLElement>('[aria-pressed="true"]')
      if (!strip || !active || strip.scrollWidth <= strip.clientWidth) return
      const box = strip.getBoundingClientRect()
      const tab = active.getBoundingClientRect()
      // A button wider than the strip is lined up by its start, so its name reads from the beginning.
      if (tab.left < box.left || tab.width > box.width) strip.scrollLeft += tab.left - box.left
      else if (tab.right > box.right) strip.scrollLeft += tab.right - box.right
    }
    keepVisible()
    // Only a change of width (rotation, window resize) re-centres: a phone's address bar hiding
    // while the page scrolls changes the height only and must not undo a sideways swipe.
    let width = window.innerWidth
    const onResize = () => { if (window.innerWidth !== width) { width = window.innerWidth; keepVisible() } }
    window.addEventListener('resize', onResize)
    // The chosen button changes width when its label does (" · paused") or when fonts arrive.
    const active = tabs.current?.querySelector('[aria-pressed="true"]')
    const observer = active && typeof ResizeObserver !== 'undefined' ? new ResizeObserver(keepVisible) : null
    if (active) observer?.observe(active)
    return () => { window.removeEventListener('resize', onResize); observer?.disconnect() }
  }, [selected, cameras.length])
  async function fullscreen() {
    try {
      if (document.fullscreenElement) await document.exitFullscreen()
      else if (root.current?.requestFullscreen) await root.current.requestFullscreen()
      else setError('Full screen is unavailable in this browser.')
    } catch { setError('Could not open full screen.') }
  }
  if (!cameras.length) return <p>No cameras available.</p>
  return <section ref={root} className="f-camera-view" aria-label="Cameras">
    <div className="f-camera-toolbar"><div ref={tabs} className="f-camera-tabs" role="group" aria-label="Choose camera">{cameras.map(camera => <button type="button" key={camera.id} aria-pressed={selected === camera.id} onClick={() => select(camera.id)}>{camera.name}{isPausedState(printers.find(p => p.id === camera.id)?.gcode_state) ? ' · paused' : ''}</button>)}<button type="button" aria-pressed={selected === 'wall'} onClick={() => select('wall')}>Wall</button></div><button type="button" onClick={fullscreen}>Full screen</button></div>
    {error && <p role="status">{error}</p>}
    <div className={selected === 'wall' ? 'f-camera-wall' : ''}>{cameras.filter(camera => selected === 'wall' || camera.id === selected).map(camera => {
      const printer = printers.find(p => p.id === camera.id)
      return <CameraFeed key={camera.id} camera={camera} printer={printer} job={printer ? activePrint(printer, prints) : undefined} />
    })}</div>
  </section>
}
export function FoundryCameras() {
  const { id } = useParams()
  const cameras = useCameraList()
  const { fleet, prints } = useFloorData()
  return <div className="f-page"><div className="f-row"><h1>Cameras</h1><Link className="f-button" to="/timelapses">Timelapses</Link></div>
    {cameras.isError ? <p role="alert">Could not load cameras. <button onClick={() => cameras.refetch()}>Retry</button></p> : !cameras.ready || cameras.isLoading ? <p role="status">Loading cameras…</p> : id && !cameras.data?.some(c => c.id === Number(id)) ? <p role="status">This camera is unavailable. <Link to="/cameras">View cameras</Link></p> : <CameraView key={id || 'wall-page'} cameras={cameras.data || []} printers={fleet.data} prints={prints.data} initialId={id ? Number(id) : undefined} />}
  </div>
}
