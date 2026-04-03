import { createContext, useContext, useState, useEffect } from 'react'
import { fetchAPI } from '../api'

const OrgContext = createContext({
  orgId: null,
  orgName: null,
  isAdmin: false,
  orgs: [],
  loading: true,
  switchOrg: () => {},
})

export function OrgProvider({ children }) {
  const [state, setState] = useState({
    orgId: null, orgName: null, isAdmin: false, orgs: [], loading: true,
  })

  useEffect(() => {
    const init = async () => {
      try {
        const me = await fetchAPI('/auth/me')
        const role = me?.role || 'viewer'
        const groupId = me?.group_id || null
        const isSuperadmin = role === 'admin' && !groupId

        let orgs = []
        if (isSuperadmin) {
          try {
            orgs = await fetchAPI('/orgs')
          } catch {
            orgs = []
          }
        }

        setState({
          orgId: groupId,
          orgName: me?.group_name || null,
          isAdmin: isSuperadmin,
          orgs: Array.isArray(orgs) ? orgs : [],
          loading: false,
        })
      } catch {
        setState(prev => ({ ...prev, loading: false }))
      }
    }
    init()
  }, [])

  const switchOrg = (newOrgId) => {
    const org = state.orgs.find(o => o.id === newOrgId)
    setState(prev => ({
      ...prev,
      orgId: newOrgId,
      orgName: org?.name || (newOrgId === null ? 'All Organizations' : null),
    }))
  }

  return (
    <OrgContext.Provider value={{ ...state, switchOrg }}>
      {children}
    </OrgContext.Provider>
  )
}

export function useOrg() {
  return useContext(OrgContext)
}

export default OrgContext
