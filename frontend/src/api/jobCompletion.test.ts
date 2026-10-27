import {afterEach,expect,it,vi} from 'vitest'
import {jobCompletion} from './jobCompletion'
import fixture from '../test-fixtures/job-completion-wire.json'
vi.mock('./client',()=>({fetchAPI:vi.fn()}))
import {fetchAPI} from './client'
afterEach(()=>vi.clearAllMocks())
it('uses the server-owned suggestion contract and start-next request fields',async()=>{
 vi.mocked(fetchAPI).mockResolvedValue(fixture.suggestion)
 expect(await jobCompletion.suggestion(201)).toEqual(fixture.suggestion)
 expect(fetchAPI).toHaveBeenCalledWith('/printers/201/next-suggestion')
 await jobCompletion.start(201,{...fixture.request,bed_clear_confirmed:true,attestation:'owner_in_app'})
 expect(fetchAPI).toHaveBeenLastCalledWith('/printers/201/start-next',{method:'POST',body:JSON.stringify(fixture.request)})
 await jobCompletion.timeline(202)
 expect(fetchAPI).toHaveBeenLastCalledWith('/jobs/202/timeline')
})

it('polls a staged bed-clear start without repeating its POST', async () => {
  vi.useFakeTimers()
  const attempt = 'b'.repeat(32)
  vi.mocked(fetchAPI).mockResolvedValueOnce({success:false,accepted:true,state:'preparing',job_id:3,attempt_id:attempt})
    .mockResolvedValueOnce({job_id:3,phase:'uploading',retry_blocked:true,observation:null})
    .mockResolvedValueOnce({job_id:3,phase:'start_accepted',retry_blocked:true,observation:null})
  try {
    const pending = jobCompletion.start(201, {...fixture.request, bed_clear_confirmed:true, attestation:'owner_in_app'})
    await vi.runAllTimersAsync()
    expect(await pending).toMatchObject({success:true,state:'dispatched'})
    expect(vi.mocked(fetchAPI).mock.calls.filter(call => call[1]?.method === 'POST')).toHaveLength(1)
    expect(fetchAPI).toHaveBeenLastCalledWith(`/jobs/3/dispatch-status?attempt_id=${attempt}`)
  } finally { vi.useRealTimers() }
})
