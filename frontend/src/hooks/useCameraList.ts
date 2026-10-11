import { useQuery } from '@tanstack/react-query'
import { useOrg } from '../contexts/OrgContext'
import type { Camera } from '../types/printer'

export interface CameraPrincipal { id: number; group_id: number | null; role: string }
export class CameraListError extends Error {
  constructor(public status: number) { super('Unable to load cameras') }
}
export function useCameraScope() {
  const { data: principal } = useQuery<CameraPrincipal>({ queryKey: ['auth', 'me'], enabled: false })
  const { orgId } = useOrg()
  const ready = Number.isInteger(principal?.id) && (principal?.id ?? 0) > 0
  return {
    ready,
    identity: JSON.stringify([principal?.id ?? null, principal?.group_id ?? null, principal?.role ?? null, orgId]),
    queryKey: ['cameras', principal?.id ?? null, principal?.group_id ?? null, principal?.role ?? null, orgId],
  }
}
export default function useCameraList() {
  const scope = useCameraScope()
  const query = useQuery<Camera[]>({
    queryKey: scope.queryKey,
    enabled: scope.ready,
    queryFn: async ({ signal }) => {
      const response = await fetch('/api/cameras', { credentials: 'include', signal })
      if (!response.ok) throw new CameraListError(response.status)
      const data = await response.json()
      if (!Array.isArray(data)) throw new CameraListError(502)
      return data
    },
    retry: false,
    refetchInterval: 30000,
  })
  return { ...query, ...scope }
}
