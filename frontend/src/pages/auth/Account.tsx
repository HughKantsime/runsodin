import { UserRound } from 'lucide-react'
import { Card, PageHeader } from '../../components/ui'
import MFASetup from '../../components/auth/MFASetup'
import APITokenManager from '../../components/auth/APITokenManager'
import SessionManager from '../../components/auth/SessionManager'
import { useQuery } from '@tanstack/react-query'
import type { User } from '../../types'

export default function Account() {
  const { data: user } = useQuery<User>({ queryKey: ['auth', 'me'], enabled: false })
  return <div className="p-4 md:p-6" key={user?.id}>
    <PageHeader icon={UserRound} title="Account" subtitle={user?.username} />
    <div className="max-w-3xl space-y-4">
      <Card><MFASetup principalKey={user?.id} /></Card>
      <Card><APITokenManager /></Card>
      <Card><SessionManager /></Card>
    </div>
  </div>
}
