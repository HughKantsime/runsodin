import React from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import EducationReports from './EducationReports'

const api=vi.hoisted(()=>({get:vi.fn()}))
vi.mock('../../api',()=>({educationReports:{getUsageReport:api.get}}))
vi.mock('recharts',async original=>{
  const actual=await original<typeof import('recharts')>()
  return {...actual,ResponsiveContainer:({children}:any)=>React.cloneElement(children,{width:800,height:300})}
})
const clients:QueryClient[]=[]
function show(){
  const client=new QueryClient({defaultOptions:{queries:{retry:false}}});clients.push(client)
  return render(<QueryClientProvider client={client}><EducationReports/></QueryClientProvider>)
}
afterEach(()=>{cleanup();clients.splice(0).forEach(c=>c.clear());vi.clearAllMocks();vi.restoreAllMocks();vi.unstubAllGlobals()})
function data(){return {
  summary:{total_users_active:1,total_print_hours:2,total_jobs:1,approval_rate:100,rejection_rate:0},
  users:[{user_id:1,username:'Student',email:'student@example.test',role:'viewer',total_jobs_submitted:1,total_jobs_approved:1,total_jobs_rejected:0,total_jobs_completed:1,total_jobs_failed:0,total_print_hours:2,estimated_filament_grams:10,accuracy:80,total_cost:null,approval_rate:100,success_rate:100,last_activity:null}],
  daily_submissions:{},by_printer:[{id:9,name:'Owned printer',available:true,total_jobs_submitted:1,total_print_hours:2,accuracy:80}],
  by_model:[{id:null,name:null,available:false,total_jobs_submitted:1,total_print_hours:2,accuracy:null}],
}}

it('labels actual hours and estimated filament, shows accuracy and opaque resource groups',async()=>{
  api.get.mockResolvedValue(data());show()
  await screen.findByRole('heading',{name:'Usage by User'})
  expect(screen.getByText('Actual Print Hours')).toBeInTheDocument()
  expect(screen.getByRole('columnheader',{name:'Estimated Filament'})).toBeInTheDocument()
  const user=screen.getAllByText('Student').map(node=>node.closest('tr')).find(Boolean)!
  expect(within(user).getByText('10g')).toBeInTheDocument()
  expect(within(user).getByText('80%')).toBeInTheDocument()
  expect(within(user).getAllByText('Unknown', {selector: 'td'}).length).toBeGreaterThan(0)
  expect(within(user).queryByText(/Est\. /)).not.toBeInTheDocument()
  expect(screen.getByText('Owned printer')).toBeInTheDocument()
  expect(screen.getByText('Unavailable resource')).toBeInTheDocument()
  expect(screen.getByText(/job estimates shown in your organization's current currency/)).toBeInTheDocument()
})

it('does not format missing or overflowing metrics as zero or infinity',async()=>{
  const report=data();Object.assign(report.users[0],{total_print_hours:null,estimated_filament_grams:null,accuracy:null,unknown_actual_hours_jobs:2,unknown_filament_estimate_jobs:2})
  api.get.mockResolvedValue(report);show();await screen.findByRole('heading',{name:'Usage by User'})
  expect(within(screen.getAllByText('Student').map(node=>node.closest('tr')).find(Boolean)!).getAllByText('Unknown').length).toBeGreaterThanOrEqual(3)
  expect(screen.queryByText('Infinity')).not.toBeInTheDocument()
  expect(screen.queryByText(/Unknown known/)).not.toBeInTheDocument()
})

it('labels known subtotals as incomplete when other jobs have unknown metrics',async()=>{
  const report=data();Object.assign(report.users[0],{unknown_actual_hours_jobs:1,unknown_filament_estimate_jobs:2})
  api.get.mockResolvedValue(report);show();await screen.findByRole('heading',{name:'Usage by User'})
  const row=screen.getAllByText('Student').map(node=>node.closest('tr')).find(Boolean)!
  expect(within(row).getByText('2h known (1 unknown)')).toBeInTheDocument()
  expect(within(row).getByText('10g known (2 unknown)')).toBeInTheDocument()
})

it('reports a query failure and allows an explicit retry',async()=>{
  api.get.mockRejectedValueOnce(new Error('bounded report refusal')).mockResolvedValue(data())
  show();expect(await screen.findByRole('alert')).toHaveTextContent('shorter time window')
  fireEvent.click(screen.getByRole('button',{name:'Retry'}))
  await screen.findByRole('heading',{name:'Usage by User'})
  expect(api.get).toHaveBeenCalledTimes(2)
})

it('exports escaped formula-safe cells and preserves incomplete metric qualifiers',async()=>{
  const exported:string[]=[]
  vi.stubGlobal('Blob',class {constructor(parts:any[]){exported.push(parts.join(''))}})
  const OriginalURL=URL
  vi.stubGlobal('URL',class extends OriginalURL {static createObjectURL(){return 'blob:owned-export'} static revokeObjectURL(){}})
  vi.spyOn(HTMLAnchorElement.prototype,'click').mockImplementation(()=>{})
  const report=data()
  Object.assign(report.users[0],{username:'=1+1,"line\nrow"',email:'comma,email@example.test',unknown_actual_hours_jobs:2,unknown_filament_estimate_jobs:3,accuracy_samples:1,unknown_accuracy_jobs:2,estimated_filament_grams_unavailable_reason:'numeric_overflow'})
  api.get.mockResolvedValue(report);show();await screen.findByRole('heading',{name:'Usage by User'})
  fireEvent.click(screen.getByRole('button',{name:'Export CSV'}))
  expect(exported).toHaveLength(1)
  expect(exported[0]).toContain('"\'=1+1,""line\nrow"""')
  expect(exported[0]).toContain('"comma,email@example.test"')
  expect(exported[0]).toContain('"Unknown Filament Estimate Jobs"')
  expect(exported[0]).toContain('"Accuracy Samples"')
  expect(exported[0]).toContain('"numeric_overflow"')
})


it('prices users and resource groups as labeled estimates in the org currency', async () => {
  const body = data()
  body.users[0] = { ...body.users[0], total_cost: 5, currency: 'EUR', unknown_cost_jobs: 1 }
  body.by_printer = [{ id: 9, name: 'Synthetic printer', available: true, total_jobs_submitted: 2, total_print_hours: 2, accuracy: 100, total_cost: 5, currency: 'EUR', unknown_cost_jobs: 0 }]
  body.by_model = [{ id: 3, name: 'Synthetic model', available: true, total_jobs_submitted: 1, total_print_hours: 2, accuracy: 100, total_cost: 3, currency: null, unknown_cost_jobs: 0 }]
  api.get.mockResolvedValue(body); show()
  expect(await screen.findByText('Est. EUR 5.00 (1 unpriced)')).toBeInTheDocument()
  expect(within(screen.getByRole('region', { name: 'Usage by printer table' })).getByText('Est. EUR 5.00')).toBeInTheDocument()
  expect(within(screen.getByRole('region', { name: 'Usage by model table' })).getByText('Unknown (currency not set)')).toBeInTheDocument()
})
