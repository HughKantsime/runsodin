import { beforeEach, describe, expect, it, vi } from 'vitest'

const fetchAPI = vi.hoisted(() => vi.fn())
vi.mock('./client', () => ({ fetchAPI }))
vi.mock('./jobDispatch', () => ({ dispatchWithConfirmation: vi.fn() }))
vi.mock('../permissions', () => ({ clearSensitiveBrowserState: vi.fn() }))
import { printFiles, printJobs } from './jobs'
import { models } from './models'

beforeEach(() => fetchAPI.mockReset().mockResolvedValue([]))

describe('checklist request URLs', () => {
  it('bounds the model read and includes the selected organization', async () => {
    await models.list(2, 1)
    expect(fetchAPI).toHaveBeenCalledWith('/models?org_id=2&limit=1')
    await models.list(null, 1)
    expect(fetchAPI).toHaveBeenLastCalledWith('/models?limit=1')
  })
  it('leaves the ordinary model list unchanged', async () => {
    await models.list()
    expect(fetchAPI).toHaveBeenCalledWith('/models')
    await models.list(2)
    expect(fetchAPI).toHaveBeenLastCalledWith('/models?org_id=2')
  })
  it('scopes the file existence read and includes scheduled files', async () => {
    await expect(printFiles.any(2)).resolves.toBe(false)
    expect(fetchAPI).toHaveBeenCalledWith('/print-files?limit=1&include_scheduled=true&org_id=2')
    fetchAPI.mockResolvedValue([{ id: 1 }])
    await expect(printFiles.any()).resolves.toBe(true)
    expect(fetchAPI).toHaveBeenLastCalledWith('/print-files?limit=1&include_scheduled=true')
  })
  it('bounds and scopes the monitor history read', async () => {
    await printJobs.list({ limit: 1, org_id: 2 })
    expect(fetchAPI).toHaveBeenCalledWith('/print-jobs?limit=1&org_id=2')
  })
})
