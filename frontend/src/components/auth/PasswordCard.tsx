import { useRef, useState, type FormEvent } from 'react'
import { auth } from '../../api'
import { Button, Card, Input } from '../ui'

export default function PasswordCard({ hasLocalPassword, onChanged }: {
  hasLocalPassword: boolean
  onChanged: () => void
}) {
  const heading = useRef<HTMLHeadingElement>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = event.currentTarget
    const fields = new FormData(form)
    const next = String(fields.get('new_password'))
    setError('')
    setNotice('')
    if (next !== fields.get('confirm_password')) {
      setError('The new passwords do not match.')
      heading.current?.focus()
      return
    }
    setSaving(true)
    try {
      await auth.changePassword(String(fields.get('current_password')), next)
      form.reset()
      setNotice('Password changed. Your other devices were signed out.')
      onChanged()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to change your password.')
    } finally {
      setSaving(false)
      heading.current?.focus()
    }
  }

  return <Card>
    <h2 ref={heading} tabIndex={-1} className="mb-3 text-lg font-semibold">Password</h2>
    {hasLocalPassword ? <form onSubmit={submit}>
      <fieldset disabled={saving} className="space-y-3">
        <Input label="Current password" name="current_password" type="password" autoComplete="current-password" required className="min-h-11" />
        <Input label="New password" name="new_password" type="password" autoComplete="new-password" required className="min-h-11" />
        <Input label="Confirm new password" name="confirm_password" type="password" autoComplete="new-password" required className="min-h-11" />
        <Button type="submit" loading={saving} className="min-h-11">Change password</Button>
      </fieldset>
      {error && <p role="alert" className="mt-2 text-sm text-red-400">{error}</p>}
      {notice && <p role="status" className="mt-2 text-sm">{notice}</p>}
    </form> : <p>Manage your password through your single sign-on provider.</p>}
  </Card>
}
