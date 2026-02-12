import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useLicense } from '../LicenseContext'
import { groups as groupsApi } from '../api'
import { Users, Plus, Edit2, Trash2, Shield, UserCheck, Eye, X } from 'lucide-react'
import clsx from 'clsx'

const API_BASE = '/api'

const fetchUsers = async () => {
  const token = localStorage.getItem('token')
  const response = await fetch(`${API_BASE}/users`, {
    headers: { 'Authorization': `Bearer ${token}`, 'X-API-Key': import.meta.env.VITE_API_KEY }
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
          <div>
            <label className="block text-sm text-farm-400 mb-1">{user ? 'New Password (leave blank to keep)' : 'Password'}</label>
            <input type="password" value={formData.password} onChange={(e) => setFormData({ ...formData, password: e.target.value })} className="w-full bg-farm-800 border border-farm-700 rounded-lg py-2 px-3 text-sm focus:outline-none focus:border-print-500" required={!user} />
          </div>
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
              <input type="checkbox" id="is_active" checked={formData.is_active} onChange={(e) => setFormData({ ...formData, is_active: e.target.checked })} className="rounded" />
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

export default function Admin() {
  const queryClient = useQueryClient()
  const [showModal, setShowModal] = useState(false)
  const [editingUser, setEditingUser] = useState(null)

  const { data: users, isLoading } = useQuery({ queryKey: ['users'], queryFn: fetchUsers })
  const lic = useLicense()
  const hasGroups = lic.hasFeature('user_groups')
  const atUserLimit = lic.atUserLimit(users?.length || 0)
  const { data: groupsList } = useQuery({ queryKey: ['groups'], queryFn: groupsApi.list, enabled: hasGroups })
  const groupsById = Object.fromEntries((groupsList || []).map(g => [g.id, g]))

  const createUser = useMutation({
    mutationFn: async (userData) => {
      const token = localStorage.getItem('token')
      const response = await fetch(`${API_BASE}/users`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}`, 'X-API-Key': import.meta.env.VITE_API_KEY }, body: JSON.stringify(userData) })
      if (!response.ok) throw new Error('Failed to create user')
      return response.json()
    },
    onSuccess: () => { queryClient.invalidateQueries(['users']); setShowModal(false) }
  })

  const updateUser = useMutation({
    mutationFn: async ({ id, ...userData }) => {
      const token = localStorage.getItem('token')
      const response = await fetch(`${API_BASE}/users/${id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}`, 'X-API-Key': import.meta.env.VITE_API_KEY }, body: JSON.stringify(userData) })
      if (!response.ok) throw new Error('Failed to update user')
      return response.json()
    },
    onSuccess: () => { queryClient.invalidateQueries(['users']); setShowModal(false); setEditingUser(null) }
  })

  const deleteUser = useMutation({
    mutationFn: async (id) => {
      const token = localStorage.getItem('token')
      const response = await fetch(`${API_BASE}/users/${id}`, { method: 'DELETE', headers: { 'Authorization': `Bearer ${token}`, 'X-API-Key': import.meta.env.VITE_API_KEY } })
      if (!response.ok) throw new Error('Failed to delete user')
    },
    onSuccess: () => queryClient.invalidateQueries(['users'])
  })

  const handleSave = (formData) => { if (editingUser) { updateUser.mutate({ id: editingUser.id, ...formData }) } else { createUser.mutate(formData) } }
  const handleEdit = (user) => { setEditingUser(user); setShowModal(true) }
  const handleDelete = (user) => { if (confirm(`Delete user "${user.username}"? This cannot be undone.`)) deleteUser.mutate(user.id) }

  return (
    <div className="p-4 md:p-8">
      <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-3 mb-6 md:mb-8">
        <div>
          <h1 className="text-2xl md:text-3xl font-display font-bold">Admin</h1>
          <p className="text-farm-500 text-sm mt-1">Manage users and permissions</p>
        </div>
        {atUserLimit
          ? <span className="flex items-center gap-2 bg-farm-700 text-farm-400 px-4 py-2 rounded-lg font-medium text-sm cursor-not-allowed" title={`User limit reached (${lic.max_users}). Upgrade to Pro for unlimited.`}>
              <Plus size={16} /> Add User (limit: {lic.max_users})
            </span>
          : <button onClick={() => { setEditingUser(null); setShowModal(true) }} className="flex items-center gap-2 bg-print-600 hover:bg-print-500 px-4 py-2 rounded-lg font-medium transition-colors text-sm">
              <Plus size={16} /> Add User
            </button>
        }
      </div>

      <div className="bg-farm-900 rounded border border-farm-800 overflow-hidden">
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
                <tr><td colSpan={hasGroups ? 7 : 5} className="py-8 text-center text-farm-500 text-sm">Loading...</td></tr>
              ) : users?.length === 0 ? (
                <tr><td colSpan={hasGroups ? 7 : 5} className="py-8 text-center text-farm-500 text-sm">No users found</td></tr>
              ) : (
                users?.map((user) => {
                  const RoleIcon = roleIcons[user.role] || Eye
                  return (
                    <tr key={user.id} className="border-t border-farm-800">
                      <td className="py-3 px-3 md:px-4">
                        <div>
                          <p className="font-medium text-sm">{user.username}</p>
                          <p className="text-xs text-farm-500">{user.email}</p>
                        </div>
                      </td>
                      <td className="py-3 px-3 md:px-4">
                        <span className={clsx('inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs', roleColors[user.role])}>
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
    </div>
  )
}
