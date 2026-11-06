import { expect, it, vi } from 'vitest'
import { educationProgramReports as api } from './educationProgramReports'
import { fetchAPI, downloadBlob } from './client'
vi.mock('./client', () => ({fetchAPI:vi.fn(),downloadBlob:vi.fn()}))
it('encodes report filters and downloads through authenticated helpers', () => {
  api.download({start_date:'2026-10-01',end_date:'2026-10-31',term:'Fall & Winter',template:'generic'},'pdf')
  expect(downloadBlob).toHaveBeenCalledWith('/education/program-reports?start_date=2026-10-01&end_date=2026-10-31&term=Fall+%26+Winter&template=generic&format=pdf','odin-program-report.pdf')
  api.mark('class1',{user_id:3,participant:true,concentrator:false,revision:4})
  expect(fetchAPI).toHaveBeenCalledWith('/education/program-reports/classes/class1/marks/3',{method:'PUT',body:JSON.stringify({participant:true,concentrator:false,revision:4})})
})
