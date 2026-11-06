import { beforeEach, expect, it, vi } from 'vitest'
import { fetchAPI } from './client'
import { educationTraining, type Training } from './educationTraining'
vi.mock('./client', () => ({ fetchAPI: vi.fn().mockResolvedValue({}) }))
const training = { id: 'uuid', revision: 3, edit_version: 5, name: 'Safety', description: '', scope_kind: 'printer_group', scope_value: '', quiz: [], pass_mark: 80, instructor_required: true, expiry_days: null, active: true } as Training
beforeEach(() => vi.clearAllMocks())
it('reads training and roster through scoped Education endpoints', async () => {
  await educationTraining.list(7); await educationTraining.roster('uuid')
  expect(fetchAPI).toHaveBeenNthCalledWith(1, '/education/cost-centers/7/trainings')
  expect(fetchAPI).toHaveBeenNthCalledWith(2, '/education/trainings/uuid/records')
})
it('sends mutation revisions, command UUIDs and bounded assessment reasons', async () => {
  await educationTraining.create(7, training); await educationTraining.update(training, training)
  await educationTraining.quiz(training, [1]); await educationTraining.assess(training, 9, 'signoff'); await educationTraining.assess(training, 9, 'revoke')
  const calls = vi.mocked(fetchAPI).mock.calls
  expect(calls.map(c => c[0])).toEqual(['/education/cost-centers/7/trainings', '/education/trainings/uuid', '/education/trainings/uuid/quiz', '/education/trainings/uuid/students/9/signoff', '/education/trainings/uuid/students/9/revoke'])
  const bodies = calls.map(c => JSON.parse(c[1]!.body as string))
  expect(bodies.every(b => typeof b.command_id === 'string' && b.command_id.length === 36)).toBe(true)
  expect(bodies.slice(1).every(b => b.revision === 3)).toBe(true)
  expect(bodies[1].edit_version).toBe(5)
  expect(bodies[2].answers).toEqual([1]); expect(bodies[3].reason).toBe('instructor_assessment'); expect(bodies[4].reason).toBe('safety_review')
})
