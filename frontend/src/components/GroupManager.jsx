import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useLicense } from '../LicenseContext'
import { groups, users as usersApi } from '../api'
import { Plus, Edit2, Trash2, Users, X, FolderOpen, RefreshCw } from 'lucide-react'

function GroupModal({ group, operatorAdmins, onClose, onSave }) {
  const [formData, setFormData] = useState({
    name: group?.name || '',
    description: group?.description || '',
    owner_id: group?.owner_id || '',
  })

  const handleSubmit = (e) => {
    e.preventDefault()
    onSave({
      ...formData,
      owner_id: formData.owner_id ? parseInt(formData.owner_id) : null,
    })
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-end sm:items-center justify-center z-50 p-0 sm:p-4">
      <div className="bg-farm-900 rounded-t-xl sm:rounded border border-farm-800 w-full max-w-md p-4 sm:p-6 max-h-[90vh] overflow-y-auto">
        <div className="flex justify-between items-center mb-4 md:mb-6">
          <h2 className="text-lg md:text-xl font-display font-bold">{group ? 'Edit Group' : 'New Group'}</h2>
          <button onClick={onClose} className="text-farm-500 hover:text-white"><X size={20} /></button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm text-farm-400 mb-1">Name</label>
            <input type="text" value={formData.name} onChange={(e) => setFormData({ ...formData, name: e.target.value })} className="w-full bg-farm-800 border border-farm-700 rounded-lg py-2 px-3 text-sm focus:outline-none focus:border-print-500" required placeholder="e.g. Mrs. Smith's Class" />
          </div>
          <div>
            <label className="block text-sm text-farm-400 mb-1">Description (optional)</label>
            <textarea value={formData.description} onChange={(e) => setFormData({ ...formData, description: e.target.value })} className="w-full bg-farm-800 border border-farm-700 rounded-lg py-2 px-3 text-sm focus:outline-none focus:border-print-500 resize-none" rows={2} placeholder="e.g. Period 3 Engineering" />
          </div>
          <div>
            <label className="block text-sm text-farm-400 mb-1">Owner (approver)</label>
            <select value={formData.owner_id} onChange={(e) => setFormData({ ...formData, owner_id: e.target.value })} className="w-full bg-farm-800 border border-farm-700 rounded-lg py-2 px-3 text-sm focus:outline-none focus:border-print-500">
              <option value="">No owner</option>
              {operatorAdmins?.map(u => (
                <option key={u.id} value={u.id}>{u.username} ({u.role})</option>
              ))}
            </select>
            <p className="text-xs text-farm-500 mt-1">The owner receives approval requests from group members</p>
          </div>
          <div className="flex gap-3 pt-4">
            <button type="button" onClick={onClose} className="flex-1 py-2 border border-farm-700 rounded-lg hover:bg-farm-800 transition-colors text-sm">Cancel</button>
            <button type="submit" className="flex-1 py-2 bg-print-600 hover:bg-print-500 rounded-lg font-medium transition-colors text-sm">{group ? 'Save Changes' : 'Create Group'}</button>
          </div>
        </form>
      </div>
    </div>
  )
}

export default function GroupManager() {
  const queryClient = useQueryClient()
  const lic = useLicense()
  const [showModal, setShowModal] = useState(false)
  const [editingGroup, setEditingGroup] = useState(null)

  if (!lic.hasFeature('user_groups')) return null

  const { data: groupsList, isLoading } = useQuery({ queryKey: ['groups'], queryFn: groups.list })
  const { data: usersList } = useQuery({ queryKey: ['users'], queryFn: usersApi.list })

  const operatorAdmins = usersList?.filter(u => u.role === 'operator' || u.role === 'admin') || []

  const createGroup = useMutation({
    mutationFn: (data) => groups.create(data),
    onSuccess: () => { queryClient.invalidateQueries(['groups']); setShowModal(false) }
  })

  const updateGroup = useMutation({
    mutationFn: ({ id, ...data }) => groups.update(id, data),
    onSuccess: () => { queryClient.invalidateQueries(['groups']); setShowModal(false); setEditingGroup(null) }
  })

  const deleteGroup = useMutation({
    mutationFn: (id) => groups.delete(id),
    onSuccess: () => queryClient.invalidateQueries(['groups'])
  })

  const handleSave = (formData) => {
    if (editingGroup) {
      updateGroup.mutate({ id: editingGroup.id, ...formData })
    } else {
      createGroup.mutate(formData)
    }
  }

  const handleEdit = (group) => { setEditingGroup(group); setShowModal(true) }
  const handleDelete = (group) => {
    if (confirm(`Delete group "${group.name}"? Members will be unassigned.`)) deleteGroup.mutate(group.id)
  }

  return (
    <div>
      <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-3 mb-4">
        <div>
          <h2 className="text-lg font-display font-bold flex items-center gap-2"><FolderOpen size={18} /> Groups</h2>
          <p className="text-farm-500 text-sm mt-0.5">Organize users into groups with designated approvers</p>
        </div>
        <button onClick={() => { setEditingGroup(null); setShowModal(true) }} className="flex items-center gap-2 bg-print-600 hover:bg-print-500 px-3 py-1.5 rounded-lg font-medium transition-colors text-sm">
          <Plus size={14} /> New Group
        </button>
      </div>

      <div className="bg-farm-900 rounded-lg border border-farm-800 overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[500px]">
            <thead className="bg-farm-800">
              <tr>
                <th className="text-left py-3 px-3 md:px-4 text-farm-400 font-medium text-xs md:text-sm">Group</th>
                <th className="text-left py-3 px-3 md:px-4 text-farm-400 font-medium text-xs md:text-sm">Owner</th>
                <th className="text-left py-3 px-3 md:px-4 text-farm-400 font-medium text-xs md:text-sm">Members</th>
                <th className="text-right py-3 px-3 md:px-4 text-farm-400 font-medium text-xs md:text-sm">Actions</th>
              </tr>
            </thead>
            <tbody>
              {isLoading ? (
                <tr><td colSpan={4} className="py-8 text-center text-farm-500 text-sm"><div className="flex items-center justify-center gap-2"><RefreshCw size={14} className="animate-spin" />Loading...</div></td></tr>
              ) : !groupsList?.length ? (
                <tr><td colSpan={4} className="py-8 text-center text-farm-500 text-sm">No groups yet</td></tr>
              ) : (
                groupsList.map(group => (
                  <tr key={group.id} className="border-t border-farm-800 hover:bg-farm-800/50 transition-colors">
                    <td className="py-3 px-3 md:px-4">
                      <p className="font-medium text-sm">{group.name}</p>
                      {group.description && <p className="text-xs text-farm-500">{group.description}</p>}
                    </td>
                    <td className="py-3 px-3 md:px-4 text-sm text-farm-300">
                      {group.owner_username || <span className="text-farm-600">—</span>}
                    </td>
                    <td className="py-3 px-3 md:px-4">
                      <span className="inline-flex items-center gap-1 text-sm text-farm-300">
                        <Users size={13} /> {group.member_count}
                      </span>
                    </td>
                    <td className="py-3 px-3 md:px-4 text-right">
                      <div className="flex justify-end gap-1">
                        <button onClick={() => handleEdit(group)} className="p-1.5 rounded-lg hover:bg-farm-800 text-farm-400 hover:text-white transition-colors" title="Edit"><Edit2 size={14} /></button>
                        <button onClick={() => handleDelete(group)} className="p-1.5 rounded-lg hover:bg-red-900/50 text-farm-400 hover:text-red-400 transition-colors" title="Delete"><Trash2 size={14} /></button>
                      </div>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      {showModal && (
        <GroupModal
          group={editingGroup}
          operatorAdmins={operatorAdmins}
          onClose={() => { setShowModal(false); setEditingGroup(null) }}
          onSave={handleSave}
        />
      )}
    </div>
  )
}
