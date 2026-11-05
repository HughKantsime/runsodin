import { useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { fetchAPI } from '../../api/client'
import { canAccessPage, canDo, getCurrentUser } from '../../permissions'
import { isOnline, isPausedState } from '../../utils/shared'
import { displayName } from '../../utils/displayName'
import type { Printer, PrintJob, Alert } from '../../types'
import { activePrint, needsYou, number, utc } from './data'

export function mayResume(printer: Printer) {
  const user = getCurrentUser()
  return !!user && ['operator', 'admin'].includes(user.role) && canDo('printers.edit') && isPausedState(printer.gcode_state) && isOnline(printer)
    && printer.org_id !== undefined && ((user.role === 'admin' && !user.group_id) || printer.org_id === null || printer.org_id === user.group_id)
    && !!printer.api_host && ['bambu', 'moonraker', 'prusalink', 'elegoo'].includes(printer.api_type || '')
    && (printer.api_type === 'moonraker' || printer.api_type === 'elegoo' || printer.has_api_key)
}
function Resume({ printer }: { printer: Printer }) {
  const client = useQueryClient()
  const [pending, setPending] = useState(false)
  const [outcome, setOutcome] = useState('')
  const busy = useRef(false)
  async function resume() {
    if (busy.current || !mayResume(printer)) return
    busy.current = true; setPending(true); setOutcome('')
    try {
      const result = await fetchAPI<{ success?: boolean }>(`/printers/${printer.id}/resume`, { method: 'POST' })
      setOutcome(result.success === true ? 'Resume requested; waiting for printer confirmation.' : 'Resume was not confirmed.')
      client.invalidateQueries({ queryKey: ['printers'] })
      client.invalidateQueries({ queryKey: ['print-jobs'] })
    } catch { setOutcome('Resume was not confirmed. Check the printer before retrying.') }
    finally { busy.current = false; setPending(false) }
  }
  return <div><button type="button" className="f-primary" disabled={pending} onClick={resume}>{pending ? 'Requesting…' : 'Resume'}</button>{outcome && <p role="status">{outcome}</p>}</div>
}
/** Use server-written alert copy. No frontend HMS/runout decoding or guessed pause timestamp. */
export default function NeedsYouBanner({ printers, prints = [], alerts = [], cameraIds = [], now = Date.now() }: { printers: Printer[]; prints?: PrintJob[]; alerts?: Alert[]; cameraIds?: number[]; now?: number }) {
  const attention = printers.filter(needsYou)
  return <section id="needs-you" className="f-needs" aria-label="Needs you">
    {attention.map(printer => {
      const job = activePrint(printer, prints)
      const alert = alerts.filter(a => a.printer_id === printer.id && ['print_paused', 'printer_error'].includes(a.alert_type)
        && (!job?.started_at || utc(a.created_at) >= utc(job.started_at)))
        .sort((a, b) => utc(b.created_at) - utc(a.created_at))[0]
      const age = alert ? Math.floor((now - utc(alert.created_at)) / 60000) : null
      const name = printer.nickname || printer.name
      const status = isPausedState(printer.gcode_state) ? 'Paused' : 'Needs you'
      const title = `${name}${name.toLowerCase().includes(status.toLowerCase()) ? '' : ` · ${status}`}: ${alert?.message || 'Check the printer. No current cause was reported.'}`
      const detail = [job ? displayName(job) : printer.current_print_display_name,
        job && number(job.progress_percent) ? `${job.progress_percent}%` : '',
        age !== null && Number.isFinite(age) && age >= 0 ? `Reported ${age < 1 ? 'just now' : `${age} minutes ago`}` : '',
      ].filter(Boolean).join(' · ')
      return <article id={`needs-you-${printer.id}`} key={printer.id} className="f-attention">
        <div className="f-attention-copy"><h2 title={title}>{title}</h2>
          {detail && <p className="f-mono" title={detail}>{detail}</p>}
        </div>
        <div className="f-actions">{mayResume(printer) && <Resume printer={printer} />}{cameraIds.includes(printer.id) && canAccessPage('cameras') && <Link className="f-button" to={`/cameras/${printer.id}`}>Open camera</Link>}</div>
      </article>
    })}
  </section>
}
