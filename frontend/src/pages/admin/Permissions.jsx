import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Shield, RotateCcw, Save, Check, Eye, UserCog, Crown, Lock } from 'lucide-react'
import { refreshPermissions } from '../../permissions'
import ConfirmModal from '../../components/ConfirmModal'

async function fetchPerms() {
  const headers = { 'Content-Type': 'application/json' }
  const res = await fetch('/api/permissions', { headers, credentials: 'include' })
  if (!res.ok) throw new Error('Failed to load permissions')
  return res.json()
}

async function savePerms(data) {
  const headers = { 'Content-Type': 'application/json' }
  const res = await fetch('/api/permissions', { method: 'PUT', headers, credentials: 'include', body: JSON.stringify(data) })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail || 'Failed to save')
  }
  return res.json()
}

async function resetPerms() {
  const headers = { 'Content-Type': 'application/json' }
  const res = await fetch('/api/permissions/reset', { method: 'POST', headers, credentials: 'include' })
  if (!res.ok) throw new Error('Failed to reset')
  return res.json()
}

const ROLES = ['admin', 'operator', 'viewer']

const ROLE_META = {
  admin:    { icon: Crown,   color: 'text-yellow-400', label: 'Admin' },
  operator: { icon: UserCog, color: 'text-blue-400',   label: 'Operator' },
  viewer:   { icon: Eye,     color: 'text-farm-400',   label: 'Viewer' },
}

// Group pages and actions for cleaner display
const PAGE_GROUPS = [
  { label: 'Monitor', pages: ['dashboard', 'printers', 'cameras'] },
  { label: 'Work', pages: ['jobs', 'timeline', 'upload', 'maintenance'] },
  { label: 'Library', pages: ['models', 'spools'] },
  { label: 'Analyze', pages: ['analytics', 'calculator'] },
  { label: 'System', pages: ['settings', 'admin', 'branding'] },
]

const ACTION_GROUPS = [
  { label: 'Jobs', actions: ['jobs.create', 'jobs.edit', 'jobs.cancel', 'jobs.delete', 'jobs.start', 'jobs.complete'] },
  { label: 'Printers', actions: ['printers.add', 'printers.edit', 'printers.delete', 'printers.slots', 'printers.reorder'] },
  { label: 'Models', actions: ['models.create', 'models.edit', 'models.delete'] },
  { label: 'Spools', actions: ['spools.edit', 'spools.delete'] },
  { label: 'Other', actions: ['timeline.move', 'upload.upload', 'upload.schedule', 'upload.delete', 'maintenance.log', 'maintenance.tasks', 'dashboard.actions'] },
]

// These combos can never be toggled off (safety)
const LOCKED = {
  'admin:admin': true,
  'admin:settings': true,
}

function formatName(key) {
  // "jobs.create" → "Create", "printers" → "Printers", "dashboard.actions" → "Actions"
  const parts = key.split('.')
  const word = parts.length > 1 ? parts[1] : parts[0]
  return word.charAt(0).toUpperCase() + word.slice(1)
}

function Toggle({ checked, onChange, disabled, locked }) {
  if (locked) {
    return (
      <div className="flex items-center justify-center" title="Required — cannot be disabled">
        <Lock size={14} className="text-farm-600" />
      </div>
    )
  }
  return (
    <button onClick={onChange} disabled={disabled}
      className={`w-9 h-5 rounded-full transition-colors flex items-center px-0.5 ${
        checked ? 'bg-green-500/80' : 'bg-farm-700'
      } ${disabled ? 'opacity-40 cursor-not-allowed' : 'cursor-pointer hover:opacity-90'}`}>
      <div className={`w-4 h-4 rounded-full bg-white shadow transition-transform ${checked ? 'translate-x-4' : 'translate-x-0'}`} />
    </button>
  )
}

function MatrixTable({ title, groups, access, type, onToggle, saving }) {
  return (
    <div>
      <h3 className="text-sm font-medium text-farm-300 uppercase tracking-wider mb-3">{title}</h3>
      <div className="border border-farm-800 rounded-lg overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-farm-900">
              <th className="text-left px-4 py-3 text-farm-400 text-xs uppercase w-48">
                {type === 'page' ? 'Page' : 'Action'}
              </th>
              {ROLES.map(role => {
                const meta = ROLE_META[role]
                return (
                  <th key={role} className="text-center px-4 py-3 w-28">
                    <div className="flex items-center justify-center gap-1.5">
                      <meta.icon size={14} className={meta.color} />
                      <span className={`text-xs uppercase ${meta.color}`}>{meta.label}</span>
                    </div>
                  </th>
                )
              })}
            </tr>
          </thead>
          <tbody>
            {groups.map(group => {
              const items = type === 'page' ? group.pages : group.actions
              // Filter to only items that exist in the access map
              const validItems = items.filter(item => access[item] !== undefined)
              if (validItems.length === 0) return null
              return [
                <tr key={`group-${group.label}`} className="bg-farm-800/30">
                  <td colSpan={4} className="px-4 py-1.5 text-xs text-farm-500 font-medium uppercase tracking-wider">
                    {group.label}
                  </td>
                </tr>,
                ...validItems.map(item => (
                  <tr key={item} className="border-t border-farm-800/50 hover:bg-farm-800/20">
                    <td className="px-4 py-2.5 text-farm-200">
                      {type === 'page' ? formatName(item) : (
                        <span>
                          <span className="text-farm-500">{item.split('.')[0]}.</span>
                          {formatName(item)}
                        </span>
                      )}
                    </td>
                    {ROLES.map(role => {
                      const isChecked = (access[item] || []).includes(role)
                      const lockKey = `${role}:${item}`
                      const isLocked = LOCKED[lockKey]
                      return (
                        <td key={role} className="text-center px-4 py-2.5">
                          <div className="flex justify-center">
                            <Toggle
                              checked={isChecked}
                              locked={isLocked}
                              disabled={saving}
                              onChange={() => onToggle(type, item, role, !isChecked)}
                            />
                          </div>
                        </td>
                      )
                    })}
                  </tr>
                ))
              ]
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

export default function Permissions() {
  const queryClient = useQueryClient()
  const [localPageAccess, setLocalPageAccess] = useState(null)
  const [localActionAccess, setLocalActionAccess] = useState(null)
  const [dirty, setDirty] = useState(false)
  const [saveMsg, setSaveMsg] = useState('')
  const [showResetConfirm, setShowResetConfirm] = useState(false)

  const { data, isLoading } = useQuery({
    queryKey: ['permissions'],
    queryFn: fetchPerms,
  })

  useEffect(() => {
    if (data) {
      setLocalPageAccess(data.page_access)
      setLocalActionAccess(data.action_access)
      setDirty(false)
    }
  }, [data])

  const saveMutation = useMutation({
    mutationFn: savePerms,
    onSuccess: async (result) => {
      queryClient.invalidateQueries({ queryKey: ['permissions'] })
      await refreshPermissions()
      setSaveMsg('Saved')
      setDirty(false)
      setTimeout(() => setSaveMsg(''), 2000)
    },
  })

  const resetMutation = useMutation({
    mutationFn: resetPerms,
    onSuccess: async (result) => {
      setLocalPageAccess(result.page_access)
      setLocalActionAccess(result.action_access)
      queryClient.invalidateQueries({ queryKey: ['permissions'] })
      await refreshPermissions()
      setSaveMsg('Reset to defaults')
      setDirty(false)
      setTimeout(() => setSaveMsg(''), 2000)
    },
  })

  const handleToggle = (type, item, role, enabled) => {
    if (type === 'page') {
      setLocalPageAccess(prev => {
        const current = [...(prev[item] || [])]
        const next = enabled ? [...current, role] : current.filter(r => r !== role)
        return { ...prev, [item]: next }
      })
    } else {
      setLocalActionAccess(prev => {
        const current = [...(prev[item] || [])]
        const next = enabled ? [...current, role] : current.filter(r => r !== role)
        return { ...prev, [item]: next }
      })
    }
    setDirty(true)
    setSaveMsg('')
  }

  const handleSave = () => {
    saveMutation.mutate({
      page_access: localPageAccess,
      action_access: localActionAccess,
    })
  }

  const handleReset = () => {
    setShowResetConfirm(true)
  }

  if (isLoading || !localPageAccess) {
    return <div className="text-farm-500 text-center py-12">Loading permissions...</div>
  }

  const saving = saveMutation.isPending || resetMutation.isPending

  return (
    <div className="p-4 md:p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-center gap-3">
          <Shield className="text-print-400" size={24} />
          <h1 className="text-xl md:text-2xl font-display font-bold">Permissions</h1>
        </div>
        <div className="flex items-center gap-2">
          {saveMsg && (
            <span className="text-green-400 text-sm flex items-center gap-1">
              <Check size={14} /> {saveMsg}
            </span>
          )}
          <button onClick={handleReset} disabled={saving}
            className="bg-farm-800 hover:bg-farm-700 text-farm-300 px-3 py-2 rounded-lg text-sm flex items-center gap-1.5 disabled:opacity-40">
            <RotateCcw size={14} /> Reset Defaults
          </button>
          <button onClick={handleSave} disabled={!dirty || saving}
            className="bg-print-500 hover:bg-print-600 text-white px-4 py-2 rounded-lg text-sm flex items-center gap-1.5 disabled:opacity-40">
            <Save size={14} /> {saving ? 'Saving...' : 'Save Changes'}
          </button>
        </div>
      </div>

      {/* Role summary */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        {ROLES.map(role => {
          const meta = ROLE_META[role]
          const pageCount = Object.values(localPageAccess).filter(roles => roles.includes(role)).length
          const actionCount = Object.values(localActionAccess).filter(roles => roles.includes(role)).length
          return (
            <div key={role} className="bg-farm-900 border border-farm-800 rounded-lg p-4">
              <div className="flex items-center gap-2 mb-2">
                <meta.icon size={18} className={meta.color} />
                <span className={`font-medium ${meta.color}`}>{meta.label}</span>
              </div>
              <div className="text-xs text-farm-400">
                {pageCount} page{pageCount !== 1 ? 's' : ''} · {actionCount} action{actionCount !== 1 ? 's' : ''}
              </div>
            </div>
          )
        })}
      </div>

      {/* Unsaved indicator */}
      {dirty && (
        <div className="bg-yellow-500/10 border border-yellow-500/30 rounded-lg px-4 py-2 text-sm text-yellow-400 flex items-center gap-2">
          <div className="w-2 h-2 rounded-full bg-yellow-400 animate-pulse" />
          Unsaved changes — click Save to apply
        </div>
      )}

      {/* Page Access Matrix */}
      <div className="overflow-x-auto">
        <MatrixTable
          title="Page Access"
          groups={PAGE_GROUPS}
          access={localPageAccess}
          type="page"
          onToggle={handleToggle}
          saving={saving}
        />
      </div>

      {/* Action Access Matrix */}
      <div className="overflow-x-auto">
        <MatrixTable
          title="Action Permissions"
          groups={ACTION_GROUPS}
          access={localActionAccess}
          type="action"
          onToggle={handleToggle}
          saving={saving}
        />
      </div>

      {saveMutation.isError && (
        <div className="bg-red-500/10 border border-red-500/30 rounded-lg px-4 py-3 text-sm text-red-400">
          Error: {saveMutation.error?.message || 'Failed to save permissions'}
        </div>
      )}

      <ConfirmModal
        open={showResetConfirm}
        onConfirm={() => { setShowResetConfirm(false); resetMutation.mutate() }}
        onCancel={() => setShowResetConfirm(false)}
        title="Reset Permissions"
        message="Reset all permissions to factory defaults? This cannot be undone."
        confirmText="Reset to Defaults"
      />
    </div>
  )
}
