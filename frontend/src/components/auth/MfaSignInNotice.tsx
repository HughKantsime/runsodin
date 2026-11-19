import { useEffect, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'

// Password sign-in uses router state. SSO hands off through the new document's history entry.
export default function MfaSignInNotice() {
  const { state, pathname, search, hash } = useLocation()
  const navigate = useNavigate()
  const [remaining, setRemaining] = useState<number | null>(null)
  useEffect(() => {
    const result = state?.mfaCompletion ?? window.history.state?.mfaCompletion
    if (result === 'enrolled') {
      document.querySelector<HTMLAnchorElement>('a[href="#main-content"]')?.focus()
    } else if (Number.isInteger(result) && result >= 0 && result <= 3) {
      setRemaining(result)
    } else return
    navigate({pathname, search, hash}, {replace: true})
  }, [state, pathname, search, hash, navigate])

  if (remaining === null) return null
  return <p role="status" className="px-4 py-3">
    {remaining === 0 ? 'You have no recovery codes remaining.' :
      `You have ${remaining} recovery ${remaining === 1 ? 'code' : 'codes'} remaining.`} Generate a new set on the <Link to="/account" className="underline">Account page</Link>.
  </p>
}
