import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2, Calendar, X, Pause, Play, Zap } from 'lucide-react'
import toast from 'react-hot-toast'
import { reportSchedules } from '../../api'

const REPORT_TYPES = ['fleet_utilization', 'job_summary', 'filament_consumption', 'failure_analysis', 'chargeback_summary']
const FREQUENCIES = ['daily', 'weekly', 'monthly']

export default function ReportScheduleManager() {
  const queryClient = useQueryClient()
  const [showCreate, setShowCreate] = useState(false)
  const [form, setForm] = useState({ name: '', report_type: 'fleet_utilization', frequency: 'weekly', recipients: '' })

  const { data: schedules, isLoading } = useQuery({
    queryKey: ['report-schedules'],
    queryFn: reportSchedules.list,
  })

  const createSchedule = useMutation({
    mutationFn: (data) => reportSchedules.create(data),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['report-schedules'] }); setShowCreate(false); setForm({ name: '', report_type: 'fleet_utilization', frequency: 'weekly', recipients: '' }) },
  })

  const deleteSchedule = useMutation({
    mutationFn: (id) => reportSchedules.delete(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['report-schedules'] }),
  })

  const toggleSchedule = useMutation({
    mutationFn: ({ id, is_active }) => reportSchedules.update(id, { is_active }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['report-schedules'] }),
  })

  const runNow = useMutation({
    mutationFn: (id) => reportSchedules.runNow(id),
    onSuccess: () => toast.success('Report sent'),
    onError: (err) => toast.error('Failed to send report: ' + err.message),
  })

  const handleCreate = () => {
    const recipients = form.recipients.split(',').map(r => r.trim()).filter(Boolean)
    createSchedule.mutate({ ...form, recipients })
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <Calendar size={18} className="text-print-400" />
          <h2 className="text-lg font-display font-semibold">Scheduled Reports</h2>
        </div>
        <button onClick={() => setShowCreate(true)} className="flex items-center gap-1.5 px-3 py-1.5 bg-print-600 hover:bg-print-500 rounded-lg text-sm">
          <Plus size={14} /> New Schedule
        </button>
      </div>

      <p className="text-xs text-farm-500 mb-4">
        Define report schedules. Reports are generated based on the configured frequency and delivered to recipients.
      </p>

      {showCreate && (
        <div className="mb-4 p-4 bg-farm-800 rounded-lg border border-farm-700 space-y-3">
          <div className="flex items-center justify-between">
            <span className="text-sm font-medium text-farm-200">New Report Schedule</span>
            <button onClick={() => setShowCreate(false)} className="p-1 text-farm-400 hover:text-farm-200"><X size={14} /></button>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label htmlFor="rs-name" className="text-xs text-farm-400 block mb-1">Name</label>
              <input id="rs-name" type="text" value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))}
                placeholder="Weekly Jobs Summary" className="w-full bg-farm-900 border border-farm-700 rounded-lg px-3 py-2 text-sm" />
            </div>
            <div>
              <label htmlFor="rs-type" className="text-xs text-farm-400 block mb-1">Report Type</label>
              <select id="rs-type" value={form.report_type} onChange={e => setForm(f => ({ ...f, report_type: e.target.value }))}
                className="w-full bg-farm-900 border border-farm-700 rounded-lg px-3 py-2 text-sm">
                {REPORT_TYPES.map(t => <option key={t} value={t}>{t.replace(/_/g, ' ')}</option>)}
              </select>
            </div>
            <div>
              <label htmlFor="rs-freq" className="text-xs text-farm-400 block mb-1">Frequency</label>
              <select id="rs-freq" value={form.frequency} onChange={e => setForm(f => ({ ...f, frequency: e.target.value }))}
                className="w-full bg-farm-900 border border-farm-700 rounded-lg px-3 py-2 text-sm">
                {FREQUENCIES.map(f => <option key={f} value={f}>{f}</option>)}
              </select>
            </div>
            <div>
              <label htmlFor="rs-recipients" className="text-xs text-farm-400 block mb-1">Recipients (comma-separated emails)</label>
              <input id="rs-recipients" type="text" value={form.recipients} onChange={e => setForm(f => ({ ...f, recipients: e.target.value }))}
                placeholder="admin@example.com" className="w-full bg-farm-900 border border-farm-700 rounded-lg px-3 py-2 text-sm" />
            </div>
          </div>
          <button onClick={handleCreate} disabled={!form.name.trim() || createSchedule.isPending}
            aria-busy={createSchedule.isPending}
            className="px-4 py-2 bg-print-600 hover:bg-print-500 disabled:opacity-50 rounded-lg text-sm">
            {createSchedule.isPending ? 'Creating...' : 'Create Schedule'}
          </button>
        </div>
      )}

      {isLoading && <p className="text-sm text-farm-500 py-4">Loading...</p>}

      {!isLoading && (!schedules || schedules.length === 0) && (
        <p className="text-sm text-farm-500 py-4">No scheduled reports configured.</p>
      )}

      {schedules?.map(s => (
        <div key={s.id} className="flex items-center justify-between p-3 bg-farm-800 rounded-lg mb-2 border border-farm-700">
          <div className="min-w-0 flex-1">
            <div className="text-sm font-medium text-farm-100">{s.name}</div>
            <div className="text-xs text-farm-500 mt-0.5">
              {s.report_type?.replace(/_/g, ' ')} · {s.frequency}
              {s.recipients?.length > 0 && ` · ${Array.isArray(s.recipients) ? s.recipients.join(', ') : s.recipients}`}
            </div>
          </div>
          <div className="flex items-center gap-2 flex-shrink-0">
            <button
              onClick={() => runNow.mutate(s.id)}
              disabled={runNow.isPending}
              aria-busy={runNow.isPending}
              aria-label={`Run "${s.name}" now`}
              className="p-1.5 text-farm-500 hover:text-print-400 rounded-lg disabled:opacity-50"
              title="Send now"
            >
              <Zap size={14} />
            </button>
            <button
              onClick={() => toggleSchedule.mutate({ id: s.id, is_active: !s.is_active })}
              className={`p-1.5 rounded-lg ${s.is_active ? 'text-green-400 hover:bg-green-900/30' : 'text-farm-500 hover:bg-farm-700'}`}
              aria-label={s.is_active ? 'Pause schedule' : 'Activate schedule'}
            >
              {s.is_active ? <Play size={14} /> : <Pause size={14} />}
            </button>
            <button
              onClick={() => deleteSchedule.mutate(s.id)}
              className="p-1.5 text-farm-500 hover:text-red-400 rounded-lg"
              aria-label="Delete schedule"
            >
              <Trash2 size={14} />
            </button>
          </div>
        </div>
      ))}
    </div>
  )
}
