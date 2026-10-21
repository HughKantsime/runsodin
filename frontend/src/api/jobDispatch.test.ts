import { afterEach, describe, expect, it, vi } from 'vitest'
import { dispatchWithConfirmation } from './jobDispatch'
vi.mock('../permissions', () => ({ clearSensitiveBrowserState: vi.fn() }))
afterEach(() => vi.unstubAllGlobals())
const status = {job_id:53, phase:'needs_reconciliation', retry_blocked:true, observation:null}

describe('dispatch response uncertainty', () => {
  it.each([502,504,408])('reconciles HTTP %s using GET without repeating POST', async code => {
    const fetch = vi.fn().mockResolvedValueOnce(new Response('proxy timeout',{status:code}))
      .mockResolvedValueOnce(Response.json(status))
    vi.stubGlobal('fetch', fetch)
    expect(await dispatchWithConfirmation(53)).toEqual({confirmation:'unconfirmed',status})
    expect(fetch.mock.calls.map(call => [call[0],call[1]?.method || 'GET'])).toEqual([
      ['/api/jobs/53/dispatch','POST'],['/api/jobs/53/dispatch-status','GET']])
  })
  it('keeps network loss unknown when reconciliation also fails', async () => {
    const fetch = vi.fn().mockRejectedValue(new TypeError('offline'))
    vi.stubGlobal('fetch',fetch)
    expect(await dispatchWithConfirmation(53)).toEqual({confirmation:'unconfirmed'})
    expect(fetch).toHaveBeenCalledTimes(2)
  })
  it('preserves a definitive validation rejection without retry',async () => {
    const fetch = vi.fn().mockResolvedValueOnce(Response.json({detail:'Tray unavailable'},{status:400})).mockResolvedValueOnce(Response.json({...status,phase:'unconfirmed'}))
    vi.stubGlobal('fetch',fetch)
    await expect(dispatchWithConfirmation(53)).rejects.toThrow('Tray unavailable')
    expect(fetch).toHaveBeenCalledTimes(2)
  })
  it('reports a successful POST as acceptance only',async () => {
    const fetch = vi.fn().mockResolvedValue(Response.json({success:true,job_id:53}))
    vi.stubGlobal('fetch',fetch)
    expect(await dispatchWithConfirmation(53)).toEqual({confirmation:'accepted'})
    expect(fetch).toHaveBeenCalledTimes(1)
  })
  it('never interprets missing claim or malformed GET as safe to retry',async () => {
    vi.stubGlobal('fetch',vi.fn().mockRejectedValueOnce(new TypeError('lost')).mockResolvedValueOnce(Response.json({job_id:53})))
    expect(await dispatchWithConfirmation(53)).toEqual({confirmation:'unconfirmed'})
  })
})

it('retains adapter uncertainty even when the server returned 400',async () => {
  vi.stubGlobal('fetch',vi.fn().mockResolvedValueOnce(Response.json({detail:'Start not confirmed'},{status:400})).mockResolvedValueOnce(Response.json(status)))
  expect(await dispatchWithConfirmation(53)).toEqual({confirmation:'unconfirmed',status})
})
