import { useState, useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useLicense } from '../LicenseContext'
import { groups as groupsApi, users as usersApi } from '../api'
import { Users, Plus, Edit2, Trash2, Shield, UserCheck, Eye, X, RefreshCw, Search, Upload, FileSpreadsheet, CheckCircle, AlertTriangle, KeyRound } from 'lucide-react'
import clsx from 'clsx'
import toast from 'react-hot-toast'
import ConfirmModal from '../components/ConfirmModal'
import UpgradeModal from '../components/UpgradeModal'

const API_BASE = '/api'

const fetchUsers = async () => {
  const response = await fetch(`${API_BASE}/users`, {
    credentials: 'include',
  })
  if (!response.ok) throw new Error('Failed to fetch users')
  return response.json()
}

const roleIcons = { admin: Shield, operator: UserCheck, viewer: Eye }
const roleColors = { admin: 'text-red-400 bg-red-900/30', operator: 'text-print-400 bg-print-900/30', viewer: 'text-farm-400 bg-farm-800' }

function UserModal({ user, groupsList, hasGroups, onClose, onSave }) {
  const [formData, setFormData] = useState({
    username: user?.username || '',
    email: user?.email || '',
    password: '',
    role: user?.role || 'operator',
    is_active: user?.is_active ?? true,
    group_id: user?.group_id || '',
    send_welcome_email: false,
  })

  const handleSubmit = (e) => {
    e.preventDefault()
    const data = { ...formData, group_id: formData.group_id ? parseInt(formData.group_id) : null }
    onSave(data)
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-end sm:items-center justify-center z-50 p-0 sm:p-4">
      <div className="bg-farm-900 rounded-t-xl sm:rounded border border-farm-800 w-full max-w-md p-4 sm:p-6 max-h-[90vh] overflow-y-auto">
        <div className="flex justify-between items-center mb-4 md:mb-6">
          <h2 className="text-lg md:text-xl font-display font-bold">{user ? 'Edit User' : 'New User'}</h2>
          <button onClick={onClose} className="text-farm-500 hover:text-white"><X size={20} /></button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm text-farm-400 mb-1">Username</label>
            <input type="text" value={formData.username} onChange={(e) => setFormData({ ...formData, username: e.target.value })} className="w-full bg-farm-800 border border-farm-700 rounded-lg py-2 px-3 text-sm focus:outline-none focus:border-print-500" required />
          </div>
          <div>
            <label className="block text-sm text-farm-400 mb-1">Email</label>
            <input type="email" value={formData.email} onChange={(e) => setFormData({ ...formData, email: e.target.value })} className="w-full bg-farm-800 border border-farm-700 rounded-lg py-2 px-3 text-sm focus:outline-none focus:border-print-500" required />
          </div>
          {!user && formData.email && (
            <div className="flex items-center gap-2">
              <input type="checkbox" id="send_welcome" checked={formData.send_welcome_email} onChange={(e) => setFormData({ ...formData, send_welcome_email: e.target.checked })} className="rounded-lg" />
              <label htmlFor="send_welcome" className="text-sm text-farm-400">Send welcome email with auto-generated password</label>
            </div>
          )}
          {!formData.send_welcome_email && (
          <div>
            <label className="block text-sm text-farm-400 mb-1">{user ? 'New Password (leave blank to keep)' : 'Password'}</label>
            <input type="password" value={formData.password} onChange={(e) => setFormData({ ...formData, password: e.target.value })} className="w-full bg-farm-800 border border-farm-700 rounded-lg py-2 px-3 text-sm focus:outline-none focus:border-print-500" required={!user && !formData.send_welcome_email} placeholder={user ? '' : 'Min 8 characters'} />
            {!user && <p className="text-xs text-farm-500 mt-1">Min 8 characters with uppercase, lowercase, and a number</p>}
          </div>
          )}
          <div>
            <label className="block text-sm text-farm-400 mb-1">Role</label>
            <select value={formData.role} onChange={(e) => setFormData({ ...formData, role: e.target.value })} className="w-full bg-farm-800 border border-farm-700 rounded-lg py-2 px-3 text-sm focus:outline-none focus:border-print-500">
              <option value="viewer">Viewer (read-only)</option>
              <option value="operator">Operator (can schedule)</option>
              <option value="admin">Admin (full access)</option>
            </select>
          </div>
          {hasGroups && (
            <div>
              <label className="block text-sm text-farm-400 mb-1">Group</label>
              <select value={formData.group_id} onChange={(e) => setFormData({ ...formData, group_id: e.target.value })} className="w-full bg-farm-800 border border-farm-700 rounded-lg py-2 px-3 text-sm focus:outline-none focus:border-print-500">
                <option value="">No group</option>
                {groupsList?.map(g => (
                  <option key={g.id} value={g.id}>{g.name}</option>
                ))}
              </select>
            </div>
          )}
          {user && (
            <div className="flex items-center gap-2">
              <input type="checkbox" id="is_active" checked={formData.is_active} onChange={(e) => setFormData({ ...formData, is_active: e.target.checked })} className="rounded-lg" />
              <label htmlFor="is_active" className="text-sm text-farm-400">Active</label>
            </div>
          )}
          <div className="flex gap-3 pt-4">
            <button type="button" onClick={onClose} className="flex-1 py-2 border border-farm-700 rounded-lg hover:bg-farm-800 transition-colors text-sm">Cancel</button>
            <button type="submit" className="flex-1 py-2 bg-print-600 hover:bg-print-500 rounded-lg font-medium transition-colors text-sm">{user ? 'Save Changes' : 'Create User'}</button>
          </div>
        </form>
      </div>
    </div>
  )
}

function ImportUsersModal({ onClose, onImported }) {
  const fileRef = useRef(null)
  const [preview, setPreview] = useState(null)
  const [importing, setImporting] = useState(false)
  const [result, setResult] = useState(null)

  const handleFileSelect = (e) => {
    const file = e.target.files?.[0]
    if (!file) return
    setResult(null)
    const reader = new FileReader()
    reader.onload = (ev) => {
      try {
        const text = ev.target.result
        const lines = text.split('\n').filter(l => l.trim())
        if (lines.length < 2) {
          setPreview({ error: 'CSV must have a header row and at least one data row' })
          return
        }
        // Parse header
        const header = lines[0].split(',').map(h => h.trim().toLowerCase().replace(/^["']|["']$/g, ''))
        if (!header.includes('username') || !header.includes('email') || !header.includes('password')) {
          setPreview({ error: 'CSV must have columns: username, email, password' })
          return
        }
        // Parse rows
        const rows = []
        for (let i = 1; i < lines.length; i++) {
          const vals = lines[i].split(',').map(v => v.trim().replace(/^["']|["']$/g, ''))
          const row = {}
          header.forEach((h, idx) => { row[h] = vals[idx] || '' })
          if (row.username) rows.push(row)
        }
        setPreview({ file, rows, header })
      } catch {
        setPreview({ error: 'Failed to parse CSV file' })
      }
    }
    reader.readAsText(file)
  }

  const handleImport = async () => {
    if (!preview?.file) return
    setImporting(true)
    try {
      const data = await usersApi.importCsv(preview.file)
      setResult(data)
      if (data.created > 0) onImported()
    } catch (err) {
      setResult({ error: err.message })
    } finally {
      setImporting(false)
    }
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-end sm:items-center justify-center z-50 p-0 sm:p-4">
      <div className="bg-farm-900 rounded-t-xl sm:rounded border border-farm-800 w-full max-w-lg p-4 sm:p-6 max-h-[90vh] overflow-y-auto">
        <div className="flex justify-between items-center mb-4">
          <h2 className="text-lg font-display font-bold flex items-center gap-2">
            <Upload size={18} className="text-print-400" />
            Import Users from CSV
          </h2>
          <button onClick={onClose} className="text-farm-500 hover:text-white"><X size={20} /></button>
        </div>

        {/* Instructions */}
        <div className="bg-farm-800 rounded-lg p-3 mb-4 text-xs text-farm-400">
          <p className="font-medium text-farm-300 mb-1">CSV Format:</p>
          <code className="block bg-farm-900 rounded px-2 py-1 font-mono">username,email,password,role</code>
          <p className="mt-1">Role is optional (defaults to "viewer"). Valid roles: admin, operator, viewer.</p>
          <p>Passwords must be 8+ chars with uppercase, lowercase, and a number.</p>
        </div>

        {/* File picker */}
        <input ref={fileRef} type="file" accept=".csv" onChange={handleFileSelect} className="hidden" />
        <button
          onClick={() => fileRef.current?.click()}
          className="w-full px-4 py-3 rounded-lg border-2 border-dashed border-farm-700 text-farm-400 hover:border-print-500 hover:text-print-400 text-sm transition-colors mb-4"
        >
          <FileSpreadsheet size={16} className="inline mr-2" />
          {preview?.file ? preview.file.name : 'Click to select .csv file'}
        </button>

        {/* Parse error */}
        {preview?.error && (
          <div className="p-3 bg-red-900/30 border border-red-700/50 rounded-lg text-sm text-red-300 mb-4">
            {preview.error}
          </div>
        )}

        {/* Preview table */}
        {preview?.rows && (
          <div className="mb-4">
            <p className="text-sm text-farm-300 mb-2">{preview.rows.length} user{preview.rows.length !== 1 ? 's' : ''} found:</p>
            <div className="max-h-48 overflow-y-auto border border-farm-800 rounded-lg">
              <table className="w-full text-xs">
                <thead className="bg-farm-800 sticky top-0">
                  <tr>
                    <th className="py-1.5 px-2 text-left text-farm-400">Username</th>
                    <th className="py-1.5 px-2 text-left text-farm-400">Email</th>
                    <th className="py-1.5 px-2 text-left text-farm-400">Role</th>
                  </tr>
                </thead>
                <tbody>
                  {preview.rows.map((row, i) => (
                    <tr key={i} className="border-t border-farm-800">
                      <td className="py-1.5 px-2 text-farm-200">{row.username}</td>
                      <td className="py-1.5 px-2 text-farm-400">{row.email}</td>
                      <td className="py-1.5 px-2 text-farm-400">{row.role || 'viewer'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* Result */}
        {result && !result.error && (
          <div className="p-3 rounded-lg mb-4 bg-farm-800 border border-farm-700 text-sm space-y-1">
            <div className="flex items-center gap-2 text-green-400">
              <CheckCircle size={14} />
              {result.created} user{result.created !== 1 ? 's' : ''} created
            </div>
            {result.skipped > 0 && (
              <div className="text-farm-400">{result.skipped} skipped (duplicate usernames)</div>
            )}
            {result.errors?.length > 0 && (
              <div className="mt-2">
                <p className="text-amber-400 flex items-center gap-1"><AlertTriangle size={12} /> {result.errors.length} error{result.errors.length !== 1 ? 's' : ''}:</p>
                <ul className="ml-4 mt-1 text-xs text-farm-500 space-y-0.5">
                  {result.errors.slice(0, 10).map((err, i) => (
                    <li key={i}>Row {err.row}: {err.reason}</li>
                  ))}
                  {result.errors.length > 10 && <li>...and {result.errors.length - 10} more</li>}
                </ul>
              </div>
            )}
          </div>
        )}
        {result?.error && (
          <div className="p-3 bg-red-900/30 border border-red-700/50 rounded-lg text-sm text-red-300 mb-4">
            {result.error}
          </div>
        )}

        {/* Actions */}
        <div className="flex gap-3">
          <button onClick={onClose} className="flex-1 py-2 border border-farm-700 rounded-lg hover:bg-farm-800 transition-colors text-sm">
            {result ? 'Close' : 'Cancel'}
          </button>
          {!result && (
            <button
              onClick={handleImport}
              disabled={!preview?.rows || importing}
              className="flex-1 py-2 bg-print-600 hover:bg-print-500 rounded-lg font-medium transition-colors text-sm disabled:opacity-50 flex items-center justify-center gap-2"
            >
              {importing ? <RefreshCw size={14} className="animate-spin" /> : <Upload size={14} />}
              {importing ? 'Importing...' : `Import ${preview?.rows?.length || 0} Users`}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

export default function Admin() {
  const queryClient = useQueryClient()
  const [showModal, setShowModal] = useState(false)
  const [showImportModal, setShowImportModal] = useState(false)
  const [editingUser, setEditingUser] = useState(null)
  const [searchQuery, setSearchQuery] = useState('')
  const [roleFilter, setRoleFilter] = useState('')
  const [deleteTarget, setDeleteTarget] = useState(null)
  const [showUpgradeModal, setShowUpgradeModal] = useState(false)

  const { data: users, isLoading } = useQuery({ queryKey: ['users'], queryFn: fetchUsers })
  const lic = useLicense()
  const hasGroups = lic.hasFeature('user_groups')
  const atUserLimit = lic.atUserLimit(users?.length || 0)
  const { data: groupsList } = useQuery({ queryKey: ['groups'], queryFn: groupsApi.list, enabled: hasGroups })
  const groupsById = Object.fromEntries((groupsList || []).map(g => [g.id, g]))

  const createUser = useMutation({
    mutationFn: async (userData) => {
      const response = await fetch(`${API_BASE}/users`, { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(userData) })
      if (!response.ok) {
        const data = await response.json().catch(() => ({}))
        throw new Error(data.detail || 'Failed to create user')
      }
      return response.json()
    },
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['users'] }); setShowModal(false) },
    onError: (err) => toast.error(err.message),
  })

  const updateUser = useMutation({
    mutationFn: async ({ id, ...userData }) => {
      const response = await fetch(`${API_BASE}/users/${id}`, { method: 'PATCH', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(userData) })
      if (!response.ok) {
        const data = await response.json().catch(() => ({}))
        throw new Error(data.detail || 'Failed to update user')
      }
      return response.json()
    },
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['users'] }); setShowModal(false); setEditingUser(null) },
    onError: (err) => toast.error(err.message),
  })

  const deleteUser = useMutation({
    mutationFn: async (id) => {
      const response = await fetch(`${API_BASE}/users/${id}`, { method: 'DELETE', credentials: 'include' })
      if (!response.ok) throw new Error('Failed to delete user')
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['users'] })
  })

  const handleSave = (formData) => { if (editingUser) { updateUser.mutate({ id: editingUser.id, ...formData }) } else { createUser.mutate(formData) } }
  const handleEdit = (user) => { setEditingUser(user); setShowModal(true) }
  const handleDelete = (user) => { setDeleteTarget(user) }

  // Filter users by search and role
  const filteredUsers = (users || []).filter(u => {
    if (searchQuery) {
      const q = searchQuery.toLowerCase()
      if (!u.username.toLowerCase().includes(q) && !u.email?.toLowerCase().includes(q)) return false
    }
    if (roleFilter && u.role !== roleFilter) return false
    return true
  })

  return (
    <div className="p-4 md:p-6">
      <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-3 mb-6 md:mb-8">
        <div className="flex items-center gap-3">
          <Users className="text-print-400" size={24} />
          <div>
            <h1 className="text-xl md:text-2xl font-display font-bold">Admin</h1>
            <p className="text-farm-500 text-sm mt-1">Manage users and permissions</p>
          </div>
        </div>
        <div className="flex gap-2">
          <button onClick={() => setShowImportModal(true)} className="flex items-center gap-2 bg-farm-700 hover:bg-farm-600 px-4 py-2 rounded-lg font-medium transition-colors text-sm">
            <Upload size={16} /> Import CSV
          </button>
          {atUserLimit
            ? <button onClick={() => setShowUpgradeModal(true)} className="flex items-center gap-2 bg-farm-700 text-farm-400 hover:text-farm-300 px-4 py-2 rounded-lg font-medium text-sm transition-colors" title={`User limit reached (${lic.maxUsers}). Upgrade to Pro for unlimited.`}>
                <Plus size={16} /> Add User (limit: {lic.maxUsers})
              </button>
            : <button onClick={() => { setEditingUser(null); setShowModal(true) }} className="flex items-center gap-2 bg-print-600 hover:bg-print-500 px-4 py-2 rounded-lg font-medium transition-colors text-sm">
                <Plus size={16} /> Add User
              </button>
          }
        </div>
      </div>

      {/* Search and Filter */}
      <div className="flex flex-col sm:flex-row gap-3 mb-4">
        <div className="relative flex-1">
          <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-farm-500" />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search by name or email..."
            className="w-full bg-farm-800 border border-farm-700 rounded-lg py-2 pl-9 pr-3 text-sm focus:outline-none focus:border-print-500"
          />
        </div>
        <select
          value={roleFilter}
          onChange={(e) => setRoleFilter(e.target.value)}
          className="bg-farm-800 border border-farm-700 rounded-lg py-2 px-3 text-sm focus:outline-none focus:border-print-500"
        >
          <option value="">All Roles</option>
          <option value="admin">Admin</option>
          <option value="operator">Operator</option>
          <option value="viewer">Viewer</option>
        </select>
      </div>

      <div className="bg-farm-900 rounded-lg border border-farm-800 overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[550px]">
            <thead className="bg-farm-800">
              <tr>
                <th className="text-left py-3 px-3 md:px-4 text-farm-400 font-medium text-xs md:text-sm">User</th>
                <th className="text-left py-3 px-3 md:px-4 text-farm-400 font-medium text-xs md:text-sm">Role</th>
                {hasGroups && <th className="text-left py-3 px-3 md:px-4 text-farm-400 font-medium text-xs md:text-sm hidden md:table-cell">Group</th>}
                <th className="text-left py-3 px-3 md:px-4 text-farm-400 font-medium text-xs md:text-sm">Status</th>
                <th className="text-left py-3 px-3 md:px-4 text-farm-400 font-medium text-xs md:text-sm hidden md:table-cell">Last Login</th>
                <th className="text-right py-3 px-3 md:px-4 text-farm-400 font-medium text-xs md:text-sm">Actions</th>
              </tr>
            </thead>
            <tbody>
              {isLoading ? (
                <tr><td colSpan={hasGroups ? 7 : 5} className="py-8 text-center text-farm-500 text-sm"><div className="flex items-center justify-center gap-2"><RefreshCw size={14} className="animate-spin" />Loading...</div></td></tr>
              ) : filteredUsers.length === 0 ? (
                <tr><td colSpan={hasGroups ? 7 : 5} className="py-8 text-center text-farm-500 text-sm">{(searchQuery || roleFilter) ? 'No users match your filters' : 'No users found'}</td></tr>
              ) : (
                filteredUsers.map((user) => {
                  const RoleIcon = roleIcons[user.role] || Eye
                  return (
                    <tr key={user.id} className="border-t border-farm-800 hover:bg-farm-800/50 transition-colors">
                      <td className="py-3 px-3 md:px-4">
                        <div>
                          <p className="font-medium text-sm">{user.username}</p>
                          <p className="text-xs text-farm-500">{user.email}</p>
                        </div>
                      </td>
                      <td className="py-3 px-3 md:px-4">
                        <span className={clsx('inline-flex items-center gap-1 px-2 py-0.5 rounded-lg text-xs', roleColors[user.role])}>
                          <RoleIcon size={12} />
                          {user.role}
                        </span>
                      </td>
                      {hasGroups && <td className="py-3 px-3 md:px-4 text-sm text-farm-300 hidden md:table-cell">
                        {user.group_id ? (groupsById[user.group_id]?.name || '—') : <span className="text-farm-600">—</span>}
                      </td>}
                      <td className="py-3 px-3 md:px-4 text-sm">
                        {user.is_active ? <span className="text-green-400">Active</span> : <span className="text-farm-500">Disabled</span>}
                      </td>
                      <td className="py-3 px-3 md:px-4 text-farm-500 text-sm hidden md:table-cell">
                        {user.last_login ? new Date(user.last_login).toLocaleDateString() : 'Never'}
                      </td>
                      <td className="py-3 px-3 md:px-4">
                        <div className="flex justify-end gap-1">
                          <button onClick={() => handleEdit(user)} className="p-1.5 md:p-2 hover:bg-farm-800 rounded-lg transition-colors" title="Edit"><Edit2 size={14} /></button>
                          {user.email && <button onClick={async () => {
                            try { await usersApi.resetPasswordEmail(user.id); toast.success('Password reset email sent') }
                            catch { toast.error('Failed — is SMTP configured?') }
                          }} className="p-1.5 md:p-2 hover:bg-farm-800 rounded-lg transition-colors text-farm-400" title="Reset password & email"><KeyRound size={14} /></button>}
                          <button onClick={() => handleDelete(user)} className="p-1.5 md:p-2 hover:bg-red-900/50 text-farm-400 hover:text-red-400 rounded-lg transition-colors" title="Delete"><Trash2 size={14} /></button>
                        </div>
                      </td>
                    </tr>
                  )
                })
              )}
            </tbody>
          </table>
        </div>
      </div>

      {showModal && (
        <UserModal user={editingUser} groupsList={groupsList} hasGroups={hasGroups} onClose={() => { setShowModal(false); setEditingUser(null) }} onSave={handleSave} />
      )}

      {showImportModal && (
        <ImportUsersModal
          onClose={() => setShowImportModal(false)}
          onImported={() => queryClient.invalidateQueries({ queryKey: ['users'] })}
        />
      )}

      <ConfirmModal
        open={!!deleteTarget}
        onConfirm={() => { deleteUser.mutate(deleteTarget.id); setDeleteTarget(null) }}
        onCancel={() => setDeleteTarget(null)}
        title="Delete User"
        message={deleteTarget ? `Delete user "${deleteTarget.username}"? This cannot be undone.` : ''}
        confirmText="Delete"
      />
      <UpgradeModal isOpen={showUpgradeModal} onClose={() => setShowUpgradeModal(false)} resource="users" />
    </div>
  )
}
