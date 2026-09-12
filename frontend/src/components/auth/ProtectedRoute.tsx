import { useQuery } from '@tanstack/react-query'
import { Navigate, useLocation } from 'react-router-dom'
import { Loader2 } from 'lucide-react'
import { getCurrentUser, refreshPermissions } from '../../permissions'

async function fetchCurrentUser() {
  const permissions = await refreshPermissions()
  const user = getCurrentUser()
  if (!permissions || !user) throw new Error('Not authenticated')
  return user
}

interface ProtectedRouteProps {
  children: React.ReactNode
}

export default function ProtectedRoute({ children }: ProtectedRouteProps) {
  const location = useLocation()

  const { data: user, isLoading, isError } = useQuery({
    queryKey: ['auth', 'me'],
    queryFn: fetchCurrentUser,
    staleTime: 5 * 60 * 1000,
    retry: false,
    refetchOnWindowFocus: true,
  })

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-screen bg-[var(--brand-bg)]">
        <Loader2 className="w-8 h-8 animate-spin text-[var(--brand-text-muted)]" />
      </div>
    )
  }

  if (isError || !user) {
    return <Navigate to="/login" state={{ from: location }} replace />
  }

  return children
}
