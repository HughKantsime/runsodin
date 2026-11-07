import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { fetchAPI } from '../../api/client'
import { users } from '../../api/auth'

export default function ControlledAudience() {
  const client = useQueryClient()
  const [id, setId] = useState<number | null>(null)
  const [name, setName] = useState('')
  const [members, setMembers] = useState<number[]>([])
  const [error, setError] = useState('')
  const accounts = useQuery({ queryKey: ['users'], queryFn: users.list })
  const audiences = useQuery({ queryKey: ['controlled-part-groups'], queryFn: () => fetchAPI<{ id: number; name: string }[]>('/controlled-parts/audiences') })
  const save = useMutation({ mutationFn: () => fetchAPI(`/controlled-parts/audiences${id ? `/${id}` : ''}`, {
    method: id ? 'PUT' : 'POST', body: JSON.stringify({ name, user_ids: members }),
  }), onSuccess: () => { client.invalidateQueries({ queryKey: ['controlled-part-groups'] }); setName(''); setMembers([]); setId(null) } })
  const remove = useMutation({ mutationFn: () => fetchAPI(`/controlled-parts/audiences/${id}`, { method: 'DELETE' }),
    onSuccess: () => { client.invalidateQueries({ queryKey: ['controlled-part-groups'] }); setId(null); setName(''); setMembers([]) } })
  return <details><summary>Manage controlled audiences</summary>
    <p>Membership grants access only within each user’s existing organization access. Administrators must be listed explicitly.</p>
    <select aria-label="Edit audience" value={id ?? ''} onChange={async event => {
      const next = Number(event.target.value); setId(next || null); setError('')
      if (!next) { setName(''); setMembers([]); return }
      try { const row = await fetchAPI<{ name: string; user_ids: number[] }>(`/controlled-parts/audiences/${next}`); setName(row.name); setMembers(row.user_ids) }
      catch (e) { setError(String(e)) }
    }}><option value="">New audience</option>{audiences.data?.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}</select>
    <label>Audience name <input value={name} maxLength={100} onChange={e => setName(e.target.value)} /></label>
    <fieldset><legend>Members</legend>{accounts.data?.filter(user => user.is_active).map(user => <label key={user.id} className="block">
      <input type="checkbox" checked={members.includes(user.id)} onChange={e => setMembers(e.target.checked ? [...members, user.id] : members.filter(uid => uid !== user.id))} /> {user.username}
    </label>)}</fieldset>
    <button type="button" disabled={!name.trim() || save.isPending} onClick={() => save.mutate()}>Save audience</button>
    {id && <button type="button" disabled={remove.isPending} onClick={() => remove.mutate()}>Delete unused audience</button>}
    {(error || save.isError || remove.isError || accounts.isError) && <p role="alert">{error || save.error?.message || remove.error?.message || accounts.error?.message}</p>}
  </details>
}
