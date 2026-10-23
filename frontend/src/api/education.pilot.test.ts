import { beforeEach, expect, it, vi } from 'vitest'
import { education } from './education'
const request = vi.hoisted(() => vi.fn().mockResolvedValue({ items: [] }))
vi.mock('./client', () => ({ fetchAPI: request }))
beforeEach(() => request.mockClear())
it('passes the selected school for platform admin listings and roster writes', async () => {
  await education.listCenters({ orgId: 7 })
  await education.listGrants(3, undefined, 7)
  await education.listCenterPrinters(3, undefined, 7)
  expect(request.mock.calls.slice(0, 3).every(call => call[0].includes('org_id=7'))).toBe(true)
  await education.replaceGrants(3, { org_id: 7, revision: 1, grants: [], command_id: 'command' })
  await education.replaceCenterPrinters(3, { org_id: 7, revision: 1, printer_ids: [], command_id: 'command' })
  expect(request.mock.calls.slice(3).every(call => JSON.parse(call[1].body).org_id === 7)).toBe(true)
})
