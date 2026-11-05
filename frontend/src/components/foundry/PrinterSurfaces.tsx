import type { ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { printJobs, jobs } from '../../api'
import { canAccessPage } from '../../permissions'
import type { Printer } from '../../types'
import PrinterCard from './PrinterCard'
import PrinterRows from './PrinterRows'
import { activePrint, nextJob, useNow, readQueue } from './data'
import useCameraList from '../../hooks/useCameraList'
import CameraView from './CameraView'

export function usePrinterJobs() {
  const prints = useQuery({ queryKey: ['print-jobs', 'foundry-running'], queryFn: () => printJobs.list({ status: 'running', limit: 1000 }), refetchInterval: 30000 })
  const queue = useQuery({ queryKey: ['jobs', 'foundry'], queryFn: readQueue, enabled: canAccessPage('jobs') || canAccessPage('timeline'), refetchInterval: 30000 })
  return { prints: prints.data || [], queue: queue.data || [], error: prints.isError || queue.isError }
}
export function ManagedPrinter({ printer, children }: { printer: Printer; children: ReactNode }) {
  const { prints, queue } = usePrinterJobs()
  const now = useNow()
  return <PrinterCard printer={printer} job={activePrint(printer, prints)} next={nextJob(printer, queue)} now={now}>
    <details className="f-management"><summary>Controls and settings</summary>{children}</details>
  </PrinterCard>
}
export function ManagedRows({ printers }: { printers: Printer[] }) {
  const { prints, queue } = usePrinterJobs()
  const now = useNow()
  return <PrinterRows printers={printers} prints={prints} queue={queue} now={now} />
}
export function PrinterDetailOverview({ printer }: { printer: Printer }) {
  const { prints, queue } = usePrinterJobs()
  const cameras = useCameraList()
  const now = useNow()
  const available = !cameras.isError && (cameras.data || []).filter(c => c.id === printer.id)
  return <div className="f-detail-stack">
    {canAccessPage('cameras') && available && available.length > 0 && <CameraView cameras={available} printers={[printer]} prints={prints} />}
    <PrinterCard printer={printer} job={activePrint(printer, prints)} next={nextJob(printer, queue)} now={now} slotVariant="full" />
  </div>
}
