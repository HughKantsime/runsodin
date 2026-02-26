import { useState, useEffect } from 'react'
import { Gauge, Save, Loader2, AlertTriangle } from 'lucide-react'
import { quotas } from '../../api'

export default function QuotaManager() {
  const [users, setUsers] = useState([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(null)
  const [error, setError] = useState('')
  const [edits, setEdits] = useState({})

  useEffect(() => {
    quotas.adminList().then(data => {
      setUsers(data)
      setLoading(false)
    }).catch(() => setLoading(false))
  }, [])

  const handleEdit = (userId, field, value) => {
    setEdits(prev => ({
      ...prev,
      [userId]: { ...(prev[userId] || {}), [field]: value }
    }))
  }

  const handleSave = async (userId) => {
    const edit = edits[userId]
    if (!edit) return
    setSaving(userId)
    setError('')
    try {
      const payload = {}
      for (const [k, v] of Object.entries(edit)) {
        payload[k] = v === '' ? null : (k === 'quota_period' ? v : Number(v))
      }
      await quotas.adminSet(userId, payload)
      setUsers(prev => prev.map(u => u.user_id === userId ? { ...u, ...payload } : u))
      setEdits(prev => { const n = { ...prev }; delete n[userId]; return n })
    } catch (err) {
      setError(err.message)
    }
    setSaving(null)
  }

  const getVal = (user, field) => {
    if (edits[user.user_id] && edits[user.user_id][field] !== undefined) return edits[user.user_id][field]
    return user[field] ?? ''
  }

  const pct = (used, limit) => {
    if (!limit) return 0
    return Math.min(100, Math.round((used / limit) * 100))
  }

  if (loading) return null

  return (
    <div>
      <div className="flex items-center gap-3 mb-4">
        <Gauge size={20} className="text-print-400" />
        <h3 className="text-lg font-semibold">Print Quotas</h3>
      </div>
      <p className="text-sm text-farm-400 mb-4">Set per-user limits on jobs, filament, or print hours per period.</p>

      {error && (
        <div className="mb-4 p-3 bg-red-900/30 border border-red-700 rounded-lg flex items-center gap-2 text-sm text-red-400">
          <AlertTriangle size={16} /> {error}
        </div>
      )}

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-farm-400 text-left border-b border-farm-700">
              <th className="py-2 pr-3">User</th>
              <th className="py-2 px-2">Jobs</th>
              <th className="py-2 px-2">Grams</th>
              <th className="py-2 px-2">Hours</th>
              <th className="py-2 px-2">Period</th>
              <th className="py-2 px-2">Usage</th>
              <th className="py-2 pl-2"></th>
            </tr>
          </thead>
          <tbody>
            {users.map(u => (
              <tr key={u.user_id} className="border-b border-farm-800">
                <td className="py-2 pr-3 font-medium">{u.username}</td>
                <td className="py-2 px-2">
                  <input type="number" min="0" className="w-16 bg-farm-800 border border-farm-700 rounded px-2 py-1 text-xs"
                    value={getVal(u, 'quota_jobs')} placeholder="--"
                    onChange={e => handleEdit(u.user_id, 'quota_jobs', e.target.value)} />
                </td>
                <td className="py-2 px-2">
                  <input type="number" min="0" className="w-20 bg-farm-800 border border-farm-700 rounded px-2 py-1 text-xs"
                    value={getVal(u, 'quota_grams')} placeholder="--"
                    onChange={e => handleEdit(u.user_id, 'quota_grams', e.target.value)} />
                </td>
                <td className="py-2 px-2">
                  <input type="number" min="0" className="w-16 bg-farm-800 border border-farm-700 rounded px-2 py-1 text-xs"
                    value={getVal(u, 'quota_hours')} placeholder="--"
                    onChange={e => handleEdit(u.user_id, 'quota_hours', e.target.value)} />
                </td>
                <td className="py-2 px-2">
                  <select className="bg-farm-800 border border-farm-700 rounded px-2 py-1 text-xs"
                    value={getVal(u, 'quota_period')} onChange={e => handleEdit(u.user_id, 'quota_period', e.target.value)}>
                    <option value="daily">Daily</option>
                    <option value="weekly">Weekly</option>
                    <option value="monthly">Monthly</option>
                    <option value="semester">Semester</option>
                  </select>
                </td>
                <td className="py-2 px-2">
                  {u.usage && (u.quota_jobs || u.quota_grams || u.quota_hours) ? (
                    <div className="space-y-0.5 text-[10px] text-farm-400">
                      {u.quota_jobs > 0 && <div>{u.usage.jobs_used}/{u.quota_jobs} jobs ({pct(u.usage.jobs_used, u.quota_jobs)}%)</div>}
                      {u.quota_grams > 0 && <div>{u.usage.grams_used?.toFixed(0)}/{u.quota_grams}g</div>}
                      {u.quota_hours > 0 && <div>{u.usage.hours_used?.toFixed(1)}/{u.quota_hours}h</div>}
                    </div>
                  ) : <span className="text-[10px] text-farm-500">No limit</span>}
                </td>
                <td className="py-2 pl-2">
                  {edits[u.user_id] && (
                    <button onClick={() => handleSave(u.user_id)} disabled={saving === u.user_id}
                      className="p-1.5 rounded bg-print-600 hover:bg-print-700 text-white disabled:opacity-50">
                      {saving === u.user_id ? <Loader2 size={12} className="animate-spin" /> : <Save size={12} />}
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
