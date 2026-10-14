import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import Utilization from './Utilization'
const api=vi.hoisted(()=>({stats:vi.fn(),jobs:vi.fn()}))
vi.mock('../../api',()=>({stats:{get:api.stats},printJobs:{list:api.jobs}}))
vi.mock('recharts',()=>({ResponsiveContainer:({children}:any)=><div>{children}</div>,BarChart:({data,children}:any)=><div data-testid={data[0]?.date?'daily-series':'util-series'}>{JSON.stringify(data)}{children}</div>,PieChart:({children}:any)=><div>{children}</div>,Pie:()=>null,Bar:()=>null,XAxis:()=>null,YAxis:()=>null,CartesianGrid:()=>null,Tooltip:()=>null,Cell:()=>null}))
const printers=[{id:9,name:'Synthetic Alpha',total_hours:12.5,completed_jobs:8,failed_jobs:3,utilization_pct:70,success_rate:72.7,avg_job_hours:1.5},{id:10,name:'Synthetic Beta',total_hours:7.5,completed_jobs:2,failed_jobs:2,utilization_pct:30,success_rate:50,avg_job_hours:3.75}]
beforeEach(()=>{vi.useFakeTimers({toFake:['Date']});vi.setSystemTime(new Date('2026-10-13T12:00:00Z'));api.stats.mockResolvedValue({printer_stats:printers});api.jobs.mockResolvedValue([{status:'completed',ended_at:'2026-10-10T12:00:00Z'},{status:'failed',ended_at:'2026-10-10T13:00:00Z'},{status:'completed',ended_at:'2026-10-11T12:00:00Z'}])})
afterEach(()=>{vi.useRealTimers();cleanup();vi.clearAllMocks();vi.unstubAllGlobals();vi.restoreAllMocks()})
async function mount(){const client=new QueryClient({defaultOptions:{queries:{retry:false}}});const view=render(<QueryClientProvider client={client}><Utilization/></QueryClientProvider>);await screen.findAllByText('Synthetic Alpha');return view}
function metric(label:string){return screen.getByText(label).parentElement!}
test('F10-10.3-01 actual fleet arithmetic calculates mean utilization, summed hours and outcomes',async()=>{
 await mount()
 expect(metric('Avg Utilization')).toHaveTextContent('50.0%')
 expect(metric('Total Print Hours')).toHaveTextContent('20h')
 expect(metric('Jobs Completed')).toHaveTextContent('10')
 expect(metric('Success Rate')).toHaveTextContent('66.7%')
 expect(metric('Failed Prints')).toHaveTextContent('5')
})
test('F10-10.3-02 per-printer table renders exact supplied percentages/hours/job counts',async()=>{
 await mount();const table=screen.getByRole('table');const a=within(table).getByText('Synthetic Alpha').closest('tr')!,b=within(table).getByText('Synthetic Beta').closest('tr')!
 expect(within(a).getByText('8')).toBeInTheDocument();expect(a.textContent).toContain('12.5h');expect(a.textContent).toContain('70%')
 expect(within(b).getByText('2')).toBeInTheDocument();expect(b.textContent).toContain('7.5h');expect(b.textContent).toContain('30%')
})
test('F10-10.3-01/02 changing time window sends exact days to both data sources',async()=>{
 await mount();fireEvent.change(screen.getByRole('combobox'),{target:{value:'7'}})
 await waitFor(()=>expect(api.stats).toHaveBeenLastCalledWith(7))
 await waitFor(()=>expect(api.jobs).toHaveBeenLastCalledWith({limit:500,days:7}))
})
test('F10-10.3-02 chart transformations preserve printer utilization and aggregate daily jobs',async()=>{
 await mount();await screen.findByTestId('daily-series')
 const util=JSON.parse(screen.getByTestId('util-series').childNodes[0].textContent!),daily=JSON.parse(screen.getByTestId('daily-series').childNodes[0].textContent!)
 expect(util.map((p:any)=>[p.utilization,p.hours])).toEqual([[70,12.5],[30,7.5]])
 expect(daily).toEqual([{date:'10-10',jobs:2},{date:'10-11',jobs:1}])
})
test('F10-10.3-01 fleet summary identifies advertised busiest printer',async()=>{
 await mount()
 const label=screen.queryByText(/busiest printer/i)
 expect(label).toBeInTheDocument()
 expect(label?.parentElement).toHaveTextContent('Synthetic Alpha')
})
test('F10-10.3-01 tied utilization is stable regardless of service ordering',async()=>{
 api.stats.mockResolvedValue({printer_stats:[{...printers[1],utilization_pct:70},printers[0]]})
 await mount();expect(metric('Busiest Printer')).toHaveTextContent('Synthetic Alpha')
})
test.each([{rows:[]},{rows:[{...printers[0],utilization_pct:null}]}])('F10-10.3-01 absent utilization does not invent a busiest printer',async ({rows})=>{
 api.stats.mockResolvedValue({printer_stats:rows})
 const client=new QueryClient({defaultOptions:{queries:{retry:false}}})
 render(<QueryClientProvider client={client}><Utilization/></QueryClientProvider>)
 await screen.findByText('No utilization data')
 expect(metric('Busiest Printer')).not.toHaveTextContent('Synthetic Alpha')
 client.clear()
})

test('daily selected window is half-open and preserves partial boundary days', async () => {
 api.jobs.mockResolvedValue([{status:'completed',ended_at:'2026-10-06T12:00:00Z'}, {status:'completed',ended_at:'2026-10-06T11:59:59Z'}, {status:'failed',ended_at:'2026-10-13T11:59:59Z'}, {status:'completed',ended_at:'2026-10-13T12:00:00Z'}])
 await mount();fireEvent.change(screen.getByRole('combobox'),{target:{value:'7'}})
 await waitFor(() => {
  const daily=JSON.parse(screen.getByTestId('daily-series').childNodes[0].textContent!)
  expect(daily).toEqual([{date:'10-06',jobs:1},{date:'10-13',jobs:1}])
 })
})
