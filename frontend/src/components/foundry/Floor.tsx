import { useEffect } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { canAccessPage } from '../../permissions'
import useCameraList from '../../hooks/useCameraList'
import SystemNoticesBanner from '../admin/SystemNoticesBanner'
import { useFoundry } from '../../hooks/themePreferences'
import DensityToggle from './DensityToggle'
import { Fleet } from './PrinterRows'
import NeedsYouBanner from './NeedsYouBanner'
import CameraView from './CameraView'
import { Schedule } from './ScheduleLane'
import { useFloorData, useFleet, useNow } from './data'
import { fleetCounts } from '../../utils/fleetCounts'

export function FleetSummary() {
  return useFoundry() ? <Summary /> : null
}
function Summary() {
  const fleet = useFleet()
  useNow()
  if (!fleet.data || fleet.isError) return null
  const { printing, ready, attention } = fleetCounts(fleet.data.filter(p => p.is_active))
  return <div className="f-fleet-summary f-mono"><span>{printing} printing · {ready} ready</span>{attention > 0 && canAccessPage('dashboard') && <Link to="/#needs-you">{attention} needs you</Link>}</div>
}
export default function Floor() {
  const { fleet, prints, queue, notices } = useFloorData()
  const cameras = useCameraList()
  const now = useNow()
  const location = useLocation()
  useEffect(() => { if (location.hash) document.getElementById(location.hash.slice(1))?.scrollIntoView() }, [location.hash, fleet.data])
  const printers = (fleet.data || []).filter(p => p.is_active)
  return <div className="f-page">
    <SystemNoticesBanner />
    <header className="f-row"><div><h1>Floor</h1><p className="f-secondary">Your printers, live cameras and the next 12 hours.</p></div><div className="f-actions"><DensityToggle />{canAccessPage('jobs') && <Link className="f-primary" to="/jobs">Schedule a print</Link>}</div></header>
    {fleet.isError ? <p role="alert">Could not load printers. <button onClick={() => fleet.refetch()}>Retry</button></p> : fleet.isLoading ? <p role="status">Loading printers…</p> : <>
      <NeedsYouBanner printers={printers} prints={prints.data} alerts={notices.data} cameraIds={cameras.isError ? [] : (cameras.data || []).map(c => c.id)} now={now} />
      {(prints.isError || queue.isError || notices.isError) && <p role="status">Some job or alert information could not be loaded. Available printer data is shown.</p>}
      <Fleet printers={printers} prints={prints.data} queue={queue.data} now={now} />
      {!printers.length && <p>No printers configured.</p>}
      <div className="f-floor-lower">{canAccessPage('cameras') && <section><h2>Cameras</h2>{cameras.isError ? <p role="alert">Could not load cameras. <button onClick={() => cameras.refetch()}>Retry</button></p> : <CameraView cameras={cameras.data || []} printers={printers} prints={prints.data} />}</section>}
        {canAccessPage('timeline') && <Schedule printers={printers} prints={prints.data} queue={queue.data} now={now} />}
      </div>
    </>}
  </div>
}
