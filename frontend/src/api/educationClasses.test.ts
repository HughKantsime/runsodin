import { expect, it, vi } from 'vitest'
import { educationClasses } from './educationClasses'
const fetchAPI = vi.hoisted(() => vi.fn())
vi.mock('./client', () => ({ fetchAPI }))
it('uses D2 endpoints and carries optimistic revisions', () => {
  educationClasses.link('assignment', 14, 3)
  expect(fetchAPI).toHaveBeenCalledWith('/education/classes/assignments/assignment/submissions', { method: 'POST', body: JSON.stringify({ submission_id: 14, revision: 3 }) })
  educationClasses.portfolio(undefined, 30)
  expect(fetchAPI).toHaveBeenLastCalledWith('/education/classes/portfolio?offset=30')
})
