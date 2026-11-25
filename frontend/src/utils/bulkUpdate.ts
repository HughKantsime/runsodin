import type { Dispatch, SetStateAction } from 'react'
import toast, { type ToastOptions } from 'react-hot-toast'
import type { BulkUpdateResult } from '../api/printers'

type Options = {
  item: 'printer' | 'job' | 'spool'
  selected: Set<number>
  setSelected: Dispatch<SetStateAction<Set<number>>>
  send: (ids: number[]) => Promise<BulkUpdateResult>
  nameOf: (id: number) => string | undefined
  batchSize?: number
  onAllChanged?: (count: number) => void
}

const notice: ToastOptions = {
  duration: 10000,
  style: {
    maxWidth: 'min(480px, calc(100vw - 32px))',
    overflowWrap: 'anywhere',
    maxHeight: '75vh',
    overflowY: 'auto',
  },
}

export async function runBulkUpdate({ item, selected, setSelected, send, nameOf, batchSize = selected.size, onAllChanged }: Options) {
  const ids = [...selected]
  let affected = 0
  let unchanged = 0
  const reasons = new Map<string, { count: number; names: string[] }>()

  function addReason(reason: string, count: number, skippedIds: number[]) {
    const group = reasons.get(reason) ?? { count: 0, names: [] }
    group.count += count
    for (const id of skippedIds) {
      const name = nameOf(id)
      if (name) group.names.push(name)
    }
    reasons.set(reason, group)
  }

  function summary() {
    const lines = [`${affected} ${item}${affected === 1 ? '' : 's'} updated. ${unchanged} not changed.`]
    for (const [reason, { count, names }] of reasons) {
      lines.push(`${count}${names.length ? ` (${names.join(', ')})` : ''}: ${reason}`)
    }
    return lines.join('\n')
  }

  for (let start = 0; start < ids.length; start += batchSize) {
    const batch = ids.slice(start, start + batchSize)
    let reply: BulkUpdateResult
    try {
      reply = await send(batch)
      if (!Number.isInteger(reply?.affected) || reply.affected < 0 || reply.affected > batch.length ||
          (typeof reply.skipped === 'number' && reply.skipped !== batch.length - reply.affected)) {
        throw new Error('The update result is unavailable. Refresh the list.')
      }
    } catch (error) {
      if (start === 0) throw error
      // Earlier job batches have already committed. Keep the unanswered rows selected.
      toast.error(`${summary()}\nThe next batch failed. ${error instanceof Error ? error.message : 'Refresh the list.'}`, notice)
      return
    }
    const skipped = 'skipped_items' in reply ? reply.skipped_items : Array.isArray(reply.skipped) ? reply.skipped : []
    const kept = new Set<number>()
    for (const entry of skipped) {
      const id = 'job_id' in entry ? entry.job_id : 'printer_id' in entry ? entry.printer_id : entry.spool_id
      kept.add(id)
      addReason(entry.detail, 1, [id])
    }
    const notChanged = typeof reply.skipped === 'number' ? reply.skipped : batch.length - reply.affected
    const unexplained = notChanged - kept.size
    if (unexplained > 0) {
      // With a mixed count-only result, the server does not identify these rows.
      addReason('No reason was provided.', unexplained, reply.affected === 0 ? batch.filter(id => !kept.has(id)) : [])
    }
    if (reply.affected > 0) {
      setSelected(current => {
        const next = new Set(current)
        for (const id of batch) if (!kept.has(id)) next.delete(id)
        return next
      })
    }
    affected += reply.affected
    unchanged += notChanged
  }

  if (unchanged === 0) onAllChanged?.(affected)
  else if (affected === 0) toast.error(summary(), notice)
  else toast(summary(), notice)
}
