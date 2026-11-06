import { beforeEach, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import RosterSettings from './RosterSettings'
import { roster } from '../../api/roster'
vi.mock('../../api/roster', () => ({ roster: { settings: vi.fn(), save: vi.fn(), connect: vi.fn(), clearTombstone: vi.fn(), courses: vi.fn(), preview: vi.fn(), apply: vi.fn() } }))
const saved = { id: 'connection', provider: 'google', revision: 1, enabled: true, connected: true,
  config: { client_id: 'school-client', public_origin: 'https://odin.example.test', allowed_domains: ['school.test'] } }
beforeEach(() => { vi.resetAllMocks(); vi.mocked(roster.settings).mockResolvedValue({ items: [] }) })
it('starts disabled and submits admin-entered credentials without displaying saved secrets', async () => {
  render(<RosterSettings />)
  expect(screen.getByRole('checkbox')).not.toBeChecked()
  await waitFor(() => expect(roster.settings).toHaveBeenCalled())
  fireEvent.change(screen.getByLabelText('Client ID'), { target: { value: 'school-client' } })
  fireEvent.change(screen.getByLabelText('Google OAuth client secret'), { target: { value: 'test-secret' } })
  fireEvent.change(screen.getByLabelText('Allowed email domains (comma separated)'), { target: { value: 'school.test' } })
  vi.mocked(roster.save).mockResolvedValue(saved as any)
  fireEvent.click(screen.getByRole('button', { name: 'Save roster settings' }))
  await waitFor(() => expect(roster.save).toHaveBeenCalledWith(expect.objectContaining({ enabled: false, config: expect.objectContaining({ client_secret: 'test-secret' }) })))
  expect(await screen.findByRole('status')).toHaveTextContent('Settings saved')
  expect(screen.getByLabelText('Google OAuth client secret')).toHaveValue('')
})
it('requires a visible dry run before confirmation and sends its exact receipt', async () => {
  vi.mocked(roster.settings).mockResolvedValue({ items: [saved as any] })
  vi.mocked(roster.courses).mockResolvedValue({ items: [{ id: 'course', title: 'Robotics' }] })
  vi.mocked(roster.preview).mockResolvedValue({ preview_token: 'opaque-preview', course: { title: 'Robotics' }, create_users: 1,
    add_memberships: [{ subject: 'subject', email: 'learner@school.test', role: 'student' }], revoke_memberships: [] })
  vi.mocked(roster.apply).mockResolvedValue({ class_id: 'new-class' })
  render(<RosterSettings />)
  expect(screen.queryByText('Confirm reviewed import')).not.toBeInTheDocument()
  fireEvent.click(await screen.findByText('Load provider courses'))
  fireEvent.click(await screen.findByText('Dry run roster import'))
  expect(await screen.findByText('Add learner@school.test as student')).toBeInTheDocument()
  fireEvent.click(screen.getByText('Confirm reviewed import'))
  await waitFor(() => expect(roster.apply).toHaveBeenCalledWith('connection', 'opaque-preview', []))
  expect(await screen.findByRole('status')).toHaveTextContent('Roster imported')
  expect(screen.queryByText('Confirm reviewed import')).not.toBeInTheDocument()
})
it('shows Canvas deployment fields and no Google secret field', async () => {
  render(<RosterSettings />)
  fireEvent.change(screen.getByLabelText('Roster provider'), { target: { value: 'canvas' } })
  expect(screen.getByLabelText('Deployment ID')).toBeInTheDocument()
  expect(screen.getByLabelText('Tool RSA private key (PEM)')).toBeInTheDocument()
  expect(screen.queryByLabelText('Google OAuth client secret')).not.toBeInTheDocument()
})
it('surfaces import failure without claiming success', async () => {
  vi.mocked(roster.settings).mockResolvedValue({ items: [saved as any] })
  vi.mocked(roster.courses).mockRejectedValue(new Error('Reconnect required'))
  render(<RosterSettings />)
  fireEvent.click(await screen.findByText('Load provider courses'))
  expect(await screen.findByRole('alert')).toHaveTextContent('Reconnect required')
  expect(screen.queryByText('Confirm reviewed import')).not.toBeInTheDocument()
})
it('requires each existing-account confirmation and displays grant-only and skipped entries', async () => {
  vi.mocked(roster.settings).mockResolvedValue({ items: [saved as any] })
  vi.mocked(roster.courses).mockResolvedValue({ items: [{ id: 'course', title: 'Robotics' }] })
  const links = [
    { subject: 'first', user_id: 4, mode: 'identity', username: 'learner', email: 'learner@school.test', roles: ['student'], label: 'Link existing account #4' },
    { subject: 'second', user_id: 5, mode: 'grant_only', username: 'teacher', email: 'teacher@school.test', roles: ['manager'], label: 'Link existing account #5 (grant-only; no new sign-in identity)' },
  ]
  vi.mocked(roster.preview).mockResolvedValue({ preview_token: 'opaque-preview', course: { title: 'Robotics' }, create_users: 0,
    add_memberships: [], revoke_memberships: [], account_links: links, skipped: [{ reason: 'roster_erased_subject' }] })
  vi.mocked(roster.apply).mockResolvedValue({ class_id: 'new-class' })
  render(<RosterSettings />)
  fireEvent.click(await screen.findByText('Load provider courses'))
  fireEvent.click(await screen.findByText('Dry run roster import'))
  const confirm = await screen.findByText('Confirm reviewed import')
  expect(confirm).toBeDisabled()
  expect(screen.getByText('Previously erased account')).toBeInTheDocument()
  expect(screen.getByText(/ODIN username: learner · Provider email: learner@school.test · Class role: student/)).toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('Link decision for learner'), { target: { value: 'confirm' } })
  expect(confirm).toBeDisabled()
  fireEvent.change(screen.getByLabelText('Link decision for teacher'), { target: { value: 'confirm' } })
  expect(confirm).toBeEnabled()
  fireEvent.click(confirm)
  await waitFor(() => expect(roster.apply).toHaveBeenCalledWith('connection', 'opaque-preview', [
    { subject: 'first', user_id: 4, mode: 'identity', decision: 'confirm' }, { subject: 'second', user_id: 5, mode: 'grant_only', decision: 'confirm' },
  ]))
})

it('allows declining a link and reports the skipped account after import', async () => {
  vi.mocked(roster.settings).mockResolvedValue({ items: [saved as any] })
  vi.mocked(roster.courses).mockResolvedValue({ items: [{ id: 'course', title: 'Robotics' }] })
  vi.mocked(roster.preview).mockResolvedValue({ preview_token: 'opaque-preview', course: { title: 'Robotics' }, create_users: 0,
    add_memberships: [], revoke_memberships: [], account_links: [
      { subject: 'first', user_id: 4, mode: 'grant_only', username: 'learner', email: 'learner@school.test', roles: ['student'], label: 'Link existing account #4' },
    ] })
  vi.mocked(roster.apply).mockResolvedValue({ class_id: 'new-class', skipped: [{ reason: 'roster_link_declined' }] })
  render(<RosterSettings />)
  fireEvent.click(await screen.findByText('Load provider courses'))
  fireEvent.click(await screen.findByText('Dry run roster import'))
  const confirm = await screen.findByText('Confirm reviewed import')
  expect(confirm).toBeDisabled()
  fireEvent.change(screen.getByLabelText('Link decision for learner'), { target: { value: 'decline' } })
  expect(screen.getByText('Skipped: learner@school.test — existing-account link declined.')).toBeInTheDocument()
  expect(confirm).toBeEnabled()
  fireEvent.click(confirm)
  await waitFor(() => expect(roster.apply).toHaveBeenCalledWith('connection', 'opaque-preview', [
    { subject: 'first', user_id: 4, mode: 'grant_only', decision: 'decline' },
  ]))
  expect(await screen.findByRole('status')).toHaveTextContent('Skipped 1: Existing-account link declined')
})
it('clears a preview suppression by its handle and requires a fresh preview', async () => {
  vi.mocked(roster.settings).mockResolvedValue({ items: [saved as any] })
  vi.mocked(roster.courses).mockResolvedValue({ items: [{ id: 'course', title: 'Robotics' }] })
  vi.mocked(roster.preview).mockResolvedValue({ preview_token: 'opaque-preview', course: { title: 'Robotics' }, create_users: 0,
    add_memberships: [], revoke_memberships: [], skipped: [
      { reason: 'roster_erased_subject', clear_handle: 'opaque-handle' },
      { reason: 'roster_domain_refused' },
    ] })
  vi.mocked(roster.clearTombstone).mockResolvedValue({ cleared: true })
  render(<RosterSettings />)
  fireEvent.click(await screen.findByText('Load provider courses'))
  fireEvent.click(await screen.findByText('Dry run roster import'))
  fireEvent.click(await screen.findByLabelText('Clear suppression for skipped entry 1'))
  await waitFor(() => expect(roster.clearTombstone).toHaveBeenCalledWith('connection', 'opaque-preview', 'opaque-handle'))
  expect(await screen.findByRole('status')).toHaveTextContent('Run a new dry run')
  expect(screen.queryByText('Confirm reviewed import')).not.toBeInTheDocument()
  expect(screen.queryByText('opaque-handle')).not.toBeInTheDocument()
})
