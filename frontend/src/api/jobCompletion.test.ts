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
