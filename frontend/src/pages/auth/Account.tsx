import { UserRound } from 'lucide-react'
import { useState } from 'react'
import { Card, PageHeader } from '../../components/ui'
import MFASetup from '../../components/auth/MFASetup'
import APITokenManager from '../../components/auth/APITokenManager'
import SessionManager from '../../components/auth/SessionManager'
import PasswordCard from '../../components/auth/PasswordCard'
import { useQuery } from '@tanstack/react-query'
import type { User } from '../../types'

export default function Account() {
  const { data: user } = useQuery<User>({ queryKey: ['auth', 'me'], enabled: false })
  const [sessionVersion, setSessionVersion] = useState(0)
  return <div className="p-4 md:p-6" key={user?.id}>
    <PageHeader icon={UserRound} title="Account" subtitle={user?.username} />
    <div className="max-w-3xl space-y-4">
      {user?.has_local_password !== undefined && <PasswordCard hasLocalPassword={user.has_local_password}
        onChanged={() => setSessionVersion(version => version + 1)} />}
      <Card><MFASetup principalKey={user?.id} /></Card>
      <Card><APITokenManager /></Card>
      <Card><SessionManager key={sessionVersion} /></Card>
    </div>
  </div>
}
