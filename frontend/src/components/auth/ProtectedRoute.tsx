import { useQuery } from '@tanstack/react-query'
import { Navigate, useLocation } from 'react-router-dom'
import { Loader2 } from 'lucide-react'

async function fetchCurrentUser() {
  const response = await fetch('/api/auth/me', { credentials: 'include' })
  if (!response.ok) throw new Error('Not authenticated')
  return response.json()
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
