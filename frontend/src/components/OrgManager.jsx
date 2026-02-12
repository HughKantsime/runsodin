import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2, Users, UserPlus, Printer, Building2, X } from 'lucide-react'
import { orgs } from '../api'
import { canDo } from '../permissions'

export default function OrgManager() {
  const queryClient = useQueryClient()
  const [showCreate, setShowCreate] = useState(false)
  const [name, setName] = useState('')
  const [addMemberId, setAddMemberId] = useState({})
  const [addPrinterId, setAddPrinterId] = useState({})

  const { data: orgList, isLoading } = useQuery({
    queryKey: ['orgs'],
    queryFn: orgs.list,
  })

  const { data: userList } = useQuery({
    queryKey: ['users-for-orgs'],
    queryFn: async () => {
      const token = localStorage.getItem('token')
      const res = await fetch('/api/users', {
        headers: { 'Authorization': `Bearer ${token}` }
      })
      return res.ok ? res.json() : []
    },
  })

  const { data: printerList } = useQuery({
    queryKey: ['printers-for-orgs'],
    queryFn: async () => {
      const token = localStorage.getItem('token')
      const API_KEY = import.meta.env.VITE_API_KEY
      const res = await fetch('/api/printers', {
        headers: { 'Authorization': `Bearer ${token}`, 'X-API-Key': API_KEY }
      })
      return res.ok ? res.json() : []
    },
  })

  const createOrg = useMutation({
    mutationFn: (data) => orgs.create(data),
    onSuccess: () => { queryClient.invalidateQueries(['orgs']); setShowCreate(false); setName('') },
  })

  const deleteOrg = useMutation({
    mutationFn: (id) => orgs.delete(id),
    onSuccess: () => queryClient.invalidateQueries(['orgs']),
  })

  const addMember = useMutation({
    mutationFn: ({ orgId, userId }) => orgs.addMember(orgId, userId),
    onSuccess: () => queryClient.invalidateQueries(['orgs']),
  })

  const assignPrinter = useMutation({
    mutationFn: ({ orgId, printerId }) => orgs.assignPrinter(orgId, printerId),
    onSuccess: () => queryClient.invalidateQueries(['orgs']),
  })

  if (!canDo('settings.edit')) return null

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <Building2 size={18} className="text-print-400" />
          <h2 className="text-lg font-display font-semibold">Organizations</h2>
        </div>
        <button
          onClick={() => setShowCreate(true)}
          className="flex items-center gap-1.5 px-3 py-1.5 bg-print-600 hover:bg-print-500 rounded-lg text-sm"
        >
          <Plus size={14} /> New Org
        </button>
      </div>

      {showCreate && (
        <div className="mb-4 p-3 bg-farm-800 rounded-lg border border-farm-700">
          <div className="flex items-center gap-2">
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Organization name"
              className="flex-1 bg-farm-900 border border-farm-700 rounded-lg px-3 py-2 text-sm"
            />
            <button
              onClick={() => createOrg.mutate({ name })}
              disabled={!name.trim() || createOrg.isPending}
              className="px-3 py-2 bg-print-600 hover:bg-print-500 disabled:opacity-50 rounded-lg text-sm"
            >
              Create
            </button>
            <button onClick={() => setShowCreate(false)} className="p-2 text-farm-400 hover:text-farm-200">
              <X size={16} />
            </button>
          </div>
        </div>
      )}

      {isLoading && <div className="text-sm text-farm-500 py-4">Loading...</div>}

      {!isLoading && (!orgList || orgList.length === 0) && (
        <p className="text-sm text-farm-500 py-4">No organizations yet. Create one to group users and assign resources.</p>
      )}

      {orgList?.map(org => (
        <div key={org.id} className="bg-farm-800 rounded-lg p-4 mb-3 border border-farm-700">
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center gap-2">
              <Building2 size={16} className="text-print-400" />
              <span className="font-medium">{org.name}</span>
              {org.member_count != null && (
                <span className="text-xs text-farm-500">{org.member_count} members</span>
              )}
            </div>
            <button
              onClick={() => deleteOrg.mutate(org.id)}
              className="p-1.5 text-farm-500 hover:text-red-400 rounded-lg"
              aria-label="Delete organization"
            >
              <Trash2 size={14} />
            </button>
          </div>

          <div className="flex gap-2 flex-wrap">
            <div className="flex items-center gap-1.5">
              <UserPlus size={12} className="text-farm-400" />
              <select
                value={addMemberId[org.id] || ''}
                onChange={(e) => setAddMemberId(p => ({ ...p, [org.id]: e.target.value }))}
                className="bg-farm-900 border border-farm-700 rounded-lg px-2 py-1 text-xs"
              >
                <option value="">Add member...</option>
                {userList?.map(u => (
                  <option key={u.id} value={u.id}>{u.username}</option>
                ))}
              </select>
              {addMemberId[org.id] && (
                <button
                  onClick={() => { addMember.mutate({ orgId: org.id, userId: parseInt(addMemberId[org.id]) }); setAddMemberId(p => ({ ...p, [org.id]: '' })) }}
                  className="px-2 py-1 bg-print-600 hover:bg-print-500 rounded-lg text-xs"
                >
                  Add
                </button>
              )}
            </div>

            <div className="flex items-center gap-1.5">
              <Printer size={12} className="text-farm-400" />
              <select
                value={addPrinterId[org.id] || ''}
                onChange={(e) => setAddPrinterId(p => ({ ...p, [org.id]: e.target.value }))}
                className="bg-farm-900 border border-farm-700 rounded-lg px-2 py-1 text-xs"
              >
                <option value="">Assign printer...</option>
                {printerList?.map(p => (
                  <option key={p.id} value={p.id}>{p.nickname || p.name}</option>
                ))}
              </select>
              {addPrinterId[org.id] && (
                <button
                  onClick={() => { assignPrinter.mutate({ orgId: org.id, printerId: parseInt(addPrinterId[org.id]) }); setAddPrinterId(p => ({ ...p, [org.id]: '' })) }}
                  className="px-2 py-1 bg-print-600 hover:bg-print-500 rounded-lg text-xs"
                >
                  Assign
                </button>
              )}
            </div>
          </div>
        </div>
      ))}
    </div>
  )
}
