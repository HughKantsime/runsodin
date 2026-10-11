import { createContext, useContext, useState, useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
const empty = { orgId: null, orgName: null, isAdmin: false, orgs: [], loading: false }
const OrgContext = createContext({ ...empty, switchOrg: (_id: number | null) => {} })
export function OrgProvider({ children }) {
  const { data: principal } = useQuery<any>({ queryKey: ['auth', 'me'], enabled: false })
  const identity = JSON.stringify([principal?.id ?? null, principal?.group_id ?? null, principal?.role ?? null])
  const ready = Number.isInteger(principal?.id) && principal.id > 0
  const [state, setState] = useState<any>({ ...empty, identity: null })
  useEffect(() => {
    const controller = new AbortController()
    let active = true
    const groupId = principal?.group_id ?? null
    const isAdmin = ready && principal.role === 'admin' && groupId === null
    if (!ready) { setState({ ...empty, identity }); return () => controller.abort() }
    setState({ ...empty, identity, orgId: groupId, isAdmin, loading: isAdmin })
    if (isAdmin) {
      fetch('/api/orgs', { credentials: 'include', signal: controller.signal })
        .then(async response => { if (!response.ok) throw new Error('Organizations unavailable'); return response.json() })
        .then(orgs => { if (active) setState({ ...empty, identity, orgId: null, isAdmin: true, orgs: Array.isArray(orgs) ? orgs : [], loading: false }) })
        .catch(() => { if (active) setState({ ...empty, identity, isAdmin: true, loading: false }) })
    }
    return () => { active = false; controller.abort() }
  }, [identity])
  // Do not expose the prior account's selected context while its effect cleans up.
  const current = state.identity === identity ? state : { ...empty, orgId: principal?.group_id ?? null, loading: ready }
  const switchOrg = (newOrgId: number | null) => {
    if (!current.isAdmin) return
    const org = current.orgs.find(item => item.id === newOrgId)
    if (newOrgId !== null && !org) return
    setState(previous => ({ ...previous, orgId: newOrgId, orgName: org?.name || 'All Organizations' }))
  }
  return <OrgContext.Provider value={{ ...current, switchOrg }}>{children}</OrgContext.Provider>
}
export function useOrg() { return useContext(OrgContext) }
export default OrgContext
