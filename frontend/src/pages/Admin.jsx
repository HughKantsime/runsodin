import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Users, Plus, Edit2, Trash2, Shield, UserCheck, Eye, X } from 'lucide-react'
import clsx from 'clsx'

// NOTE: This page is not wired up yet. To enable:
// 1. Add route in App.jsx: <Route path="/admin" element={<Admin />} />
// 2. Add NavItem in sidebar
// 3. Add API endpoints in backend
// 4. Add admin-only route protection

const API_BASE = '/api'

const fetchUsers = async () => {
  const token = localStorage.getItem('token')
  const response = await fetch(`${API_BASE}/users`, {
    headers: { 'Authorization': `Bearer ${token}`, 'X-API-Key': '5464389e808f206efd9f9febef7743ff7a16911797cb0f058e805c82b33396ce' }
  })
  if (!response.ok) throw new Error('Failed to fetch users')
  return response.json()
}

const roleIcons = {
  admin: Shield,
  operator: UserCheck,
  viewer: Eye
}

const roleColors = {
  admin: 'text-red-400 bg-red-900/30',
  operator: 'text-print-400 bg-print-900/30',
  viewer: 'text-farm-400 bg-farm-800'
}

function UserModal({ user, onClose, onSave }) {
  const [formData, setFormData] = useState({
    username: user?.username || '',
    email: user?.email || '',
    password: '',
    role: user?.role || 'operator',
    is_active: user?.is_active ?? true
  })

  const handleSubmit = (e) => {
    e.preventDefault()
    onSave(formData)
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div className="bg-farm-900 rounded-xl border border-farm-800 w-full max-w-md p-6">
        <div className="flex justify-between items-center mb-6">
          <h2 className="text-xl font-display font-bold">
            {user ? 'Edit User' : 'New User'}
          </h2>
          <button onClick={onClose} className="text-farm-500 hover:text-white">
            <X size={20} />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm text-farm-400 mb-1">Username</label>
            <input
              type="text"
              value={formData.username}
              onChange={(e) => setFormData({ ...formData, username: e.target.value })}
              className="w-full bg-farm-800 border border-farm-700 rounded-lg py-2 px-3 focus:outline-none focus:border-print-500"
              required
            />
          </div>

          <div>
            <label className="block text-sm text-farm-400 mb-1">Email</label>
            <input
              type="email"
              value={formData.email}
              onChange={(e) => setFormData({ ...formData, email: e.target.value })}
              className="w-full bg-farm-800 border border-farm-700 rounded-lg py-2 px-3 focus:outline-none focus:border-print-500"
              required
            />
          </div>

          <div>
            <label className="block text-sm text-farm-400 mb-1">
              {user ? 'New Password (leave blank to keep)' : 'Password'}
            </label>
            <input
              type="password"
              value={formData.password}
              onChange={(e) => setFormData({ ...formData, password: e.target.value })}
              className="w-full bg-farm-800 border border-farm-700 rounded-lg py-2 px-3 focus:outline-none focus:border-print-500"
              required={!user}
            />
          </div>

          <div>
            <label className="block text-sm text-farm-400 mb-1">Role</label>
            <select
              value={formData.role}
              onChange={(e) => setFormData({ ...formData, role: e.target.value })}
              className="w-full bg-farm-800 border border-farm-700 rounded-lg py-2 px-3 focus:outline-none focus:border-print-500"
            >
              <option value="viewer">Viewer (read-only)</option>
              <option value="operator">Operator (can schedule)</option>
              <option value="admin">Admin (full access)</option>
            </select>
          </div>

          {user && (
            <div className="flex items-center gap-2">
              <input
                type="checkbox"
                id="is_active"
                checked={formData.is_active}
                onChange={(e) => setFormData({ ...formData, is_active: e.target.checked })}
                className="rounded"
              />
              <label htmlFor="is_active" className="text-sm text-farm-400">Active</label>
            </div>
          )}

          <div className="flex gap-3 pt-4">
            <button
              type="button"
              onClick={onClose}
              className="flex-1 py-2 border border-farm-700 rounded-lg hover:bg-farm-800 transition-colors"
            >
              Cancel
            </button>
            <button
              type="submit"
              className="flex-1 py-2 bg-print-600 hover:bg-print-500 rounded-lg font-medium transition-colors"
            >
              {user ? 'Save Changes' : 'Create User'}
            </button>
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

  const { data: users, isLoading } = useQuery({
    queryKey: ['users'],
    queryFn: fetchUsers
  })

  const createUser = useMutation({
    mutationFn: async (userData) => {
      const token = localStorage.getItem('token')
      const response = await fetch(`${API_BASE}/users`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${token}`, 'X-API-Key': '5464389e808f206efd9f9febef7743ff7a16911797cb0f058e805c82b33396ce'
        },
        body: JSON.stringify(userData)
      })
      if (!response.ok) throw new Error('Failed to create user')
      return response.json()
    },
    onSuccess: () => {
      queryClient.invalidateQueries(['users'])
      setShowModal(false)
    }
  })

  const updateUser = useMutation({
    mutationFn: async ({ id, ...userData }) => {
      const token = localStorage.getItem('token')
      const response = await fetch(`${API_BASE}/users/${id}`, {
        method: 'PATCH',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${token}`, 'X-API-Key': '5464389e808f206efd9f9febef7743ff7a16911797cb0f058e805c82b33396ce'
        },
        body: JSON.stringify(userData)
      })
      if (!response.ok) throw new Error('Failed to update user')
      return response.json()
    },
    onSuccess: () => {
      queryClient.invalidateQueries(['users'])
      setShowModal(false)
      setEditingUser(null)
    }
  })

  const deleteUser = useMutation({
    mutationFn: async (id) => {
      const token = localStorage.getItem('token')
      const response = await fetch(`${API_BASE}/users/${id}`, {
        method: 'DELETE',
        headers: { 'Authorization': `Bearer ${token}`, 'X-API-Key': '5464389e808f206efd9f9febef7743ff7a16911797cb0f058e805c82b33396ce' }
      })
      if (!response.ok) throw new Error('Failed to delete user')
    },
    onSuccess: () => {
      queryClient.invalidateQueries(['users'])
    }
  })

  const handleSave = (formData) => {
    if (editingUser) {
      updateUser.mutate({ id: editingUser.id, ...formData })
    } else {
      createUser.mutate(formData)
    }
  }

  const handleEdit = (user) => {
    setEditingUser(user)
    setShowModal(true)
  }

  const handleDelete = (user) => {
    if (confirm(`Delete user "${user.username}"? This cannot be undone.`)) {
      deleteUser.mutate(user.id)
    }
  }

  return (
    <div className="p-8">
      <div className="flex justify-between items-center mb-8">
        <div>
          <h1 className="text-3xl font-display font-bold">Admin</h1>
          <p className="text-farm-500 mt-1">Manage users and permissions</p>
        </div>
        <button
          onClick={() => { setEditingUser(null); setShowModal(true) }}
          className="flex items-center gap-2 bg-print-600 hover:bg-print-500 px-4 py-2 rounded-lg font-medium transition-colors"
        >
          <Plus size={18} />
          Add User
        </button>
      </div>

      {/* Users Table */}
      <div className="bg-farm-900 rounded-xl border border-farm-800 overflow-hidden">
        <table className="w-full">
          <thead className="bg-farm-800">
            <tr>
              <th className="text-left py-3 px-4 text-farm-400 font-medium">User</th>
              <th className="text-left py-3 px-4 text-farm-400 font-medium">Role</th>
              <th className="text-left py-3 px-4 text-farm-400 font-medium">Status</th>
              <th className="text-left py-3 px-4 text-farm-400 font-medium">Last Login</th>
              <th className="text-right py-3 px-4 text-farm-400 font-medium">Actions</th>
            </tr>
          </thead>
          <tbody>
            {isLoading ? (
              <tr>
                <td colSpan={5} className="py-8 text-center text-farm-500">Loading...</td>
              </tr>
            ) : users?.length === 0 ? (
              <tr>
                <td colSpan={5} className="py-8 text-center text-farm-500">No users found</td>
              </tr>
            ) : (
              users?.map((user) => {
                const RoleIcon = roleIcons[user.role] || Eye
                return (
                  <tr key={user.id} className="border-t border-farm-800">
                    <td className="py-3 px-4">
                      <div>
                        <p className="font-medium">{user.username}</p>
                        <p className="text-sm text-farm-500">{user.email}</p>
                      </div>
                    </td>
                    <td className="py-3 px-4">
                      <span className={clsx('inline-flex items-center gap-1.5 px-2 py-1 rounded text-sm', roleColors[user.role])}>
                        <RoleIcon size={14} />
                        {user.role}
                      </span>
                    </td>
                    <td className="py-3 px-4">
                      {user.is_active ? (
                        <span className="text-green-400">Active</span>
                      ) : (
                        <span className="text-farm-500">Disabled</span>
                      )}
                    </td>
                    <td className="py-3 px-4 text-farm-500">
                      {user.last_login ? new Date(user.last_login).toLocaleDateString() : 'Never'}
                    </td>
                    <td className="py-3 px-4">
                      <div className="flex justify-end gap-2">
                        <button
                          onClick={() => handleEdit(user)}
                          className="p-2 hover:bg-farm-800 rounded-lg transition-colors"
                          title="Edit"
                        >
                          <Edit2 size={16} />
                        </button>
                        <button
                          onClick={() => handleDelete(user)}
                          className="p-2 hover:bg-red-900/50 text-farm-400 hover:text-red-400 rounded-lg transition-colors"
                          title="Delete"
                        >
                          <Trash2 size={16} />
                        </button>
                      </div>
                    </td>
                  </tr>
                )
              })
            )}
          </tbody>
        </table>
      </div>

      {/* Modal */}
      {showModal && (
        <UserModal
          user={editingUser}
          onClose={() => { setShowModal(false); setEditingUser(null) }}
          onSave={handleSave}
        />
      )}
    </div>
  )
}
