import { afterEach, describe, expect, it, vi } from 'vitest'

const fetchAPI = vi.hoisted(() => vi.fn())
vi.mock('./client', () => ({ fetchAPI }))
vi.mock('./jobDispatch', () => ({ dispatchWithConfirmation: vi.fn() }))
vi.mock('../permissions', () => ({ clearSensitiveBrowserState: vi.fn() }))

import { buildJobsQuery, jobs } from './jobs'

afterEach(() => vi.clearAllMocks())

describe('GET /jobs request', () => {
  it('callers that pass nothing (dashboard, apps) get the plain, unchanged URL', async () => {
    fetchAPI.mockResolvedValue([])
    await jobs.list()
    expect(fetchAPI).toHaveBeenCalledWith('/jobs', {})
    await jobs.list('pending', 17)
    expect(fetchAPI).toHaveBeenLastCalledWith('/jobs?status=pending&org_id=17', {})
    expect(buildJobsQuery()).toBe('')
  })

  it('repeats status, and sends q, printer, held, order and paging', () => {
    expect(buildJobsQuery(['pending', 'scheduled'], 17, { limit: 50, offset: 50, order: 'recent', q: 'job 218', printerId: 9, held: true }))
      .toBe('?status=pending&status=scheduled&org_id=17&printer_id=9&held=true&q=job+218&order=recent&limit=50&offset=50')
    expect(buildJobsQuery(null, null, { unassigned: true })).toBe('?unassigned=true')
  })

  it('hands the X-Total-Count header to onTotal, and null when it is missing or not a number', async () => {
    const seen: Array<number | null> = []
    fetchAPI.mockImplementation(async (_url: string, options: { onResponse?: (r: Response) => void }) => {
      options.onResponse?.(new Response('[]', { headers: { 'X-Total-Count': '212' } }))
      options.onResponse?.(new Response('[]'))
      options.onResponse?.(new Response('[]', { headers: { 'X-Total-Count': 'many' } }))
      return []
    })
    await jobs.list(['pending'], null, { limit: 50 }, total => seen.push(total))
    expect(seen).toEqual([212, null, null])
  })
})
