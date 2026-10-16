import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import SystemTab from './SystemTab'
import EnergyWidget from '../reporting/EnergyWidget'
const api=vi.hoisted(()=>({report:vi.fn(),list:vi.fn(),create:vi.fn(),update:vi.fn(),remove:vi.fn(),run:vi.fn(),download:vi.fn()}))
vi.mock('../../api',()=>({chargebacks:{report:api.report},reportSchedules:{list:api.list,create:api.create,update:api.update,delete:api.remove,runNow:api.run},fetchAPI:vi.fn().mockResolvedValue({}),config:{getNetwork:vi.fn().mockResolvedValue({})},backups:{list:vi.fn().mockResolvedValue([])},downloadBlob:api.download,getEnergyRate:vi.fn().mockResolvedValue({energy_cost_per_kwh:.2}),setEnergyRate:vi.fn()}))
vi.mock('../../permissions',()=>({clearSensitiveBrowserState:vi.fn()}))
// License fixture: chargebacks and analytics report types are Pro "analytics" features.
const lic=vi.hoisted(()=>({features:['analytics'] as string[]}))
vi.mock('../../LicenseContext',()=>({useLicense:()=>({hasFeature:(f:string)=>lic.features.includes(f)})}))
vi.mock('./BackupRestore',()=>({default:()=>null}))
vi.mock('./TroubleshootingPanel',()=>({default:()=>null}))
vi.mock('./DataRetentionSettings',()=>({default:()=>null}))
vi.mock('recharts',()=>({ResponsiveContainer:({children}:any)=><div>{children}</div>,BarChart:({data,children}:any)=><div data-testid='energy-series'>{JSON.stringify(data)}{children}</div>,Bar:()=>null,XAxis:()=>null,YAxis:()=>null,CartesianGrid:()=>null,Tooltip:()=>null}))
beforeEach(()=>{
 lic.features=['analytics']
 api.report.mockResolvedValue([{user_id:8,username:'Synthetic student',job_count:4,total_hours:2.5,total_grams:125,total_cost:7.25,currency:'USD'}])
 api.list.mockResolvedValue([{id:19,name:'Synthetic weekly report',report_type:'job_summary',frequency:'weekly',recipients:['synthetic@example.invalid'],is_active:true}])
 api.create.mockResolvedValue({});api.update.mockResolvedValue({});api.download.mockResolvedValue(undefined)
 vi.stubGlobal('matchMedia',vi.fn().mockReturnValue({matches:false}))
})
afterEach(()=>{cleanup();vi.clearAllMocks();vi.unstubAllGlobals()})
function system(){const client=new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}});render(<QueryClientProvider client={client}><SystemTab/></QueryClientProvider>)}
test('F10-10.4-02 EnergyWidget aggregates daily consumption from actual job records',async()=>{
 render(<EnergyWidget jobs={[{energy_kwh:2,ended_at:'2026-10-10 12:00:00'},{energy_kwh:3,ended_at:'2026-10-10 14:00:00'},{energy_kwh:1,ended_at:'2026-10-11 12:00:00'}]}/>)
 await screen.findByRole('button',{name:/\$0.2\/kWh/})
 const data=JSON.parse(screen.getByTestId('energy-series').childNodes[0].textContent!)
 expect(data).toEqual([{day:'Oct 10',kwh:5,cost:1},{day:'Oct 11',kwh:1,cost:.2}])
 expect(screen.getByText('$1.20')).toBeInTheDocument()
})
test.each([['Jobs','/export/jobs','jobs_export.csv'],['Models','/export/models','models_export.csv'],['Spools','/export/spools','spools_export.csv'],['Filament Usage','/export/filament-usage','filament_usage_export.csv']])('F10-10.6-02 System export %s forwards exact endpoint and filename',async(label,path,filename)=>{
 system();fireEvent.click(screen.getByRole('button',{name:label,exact:true}))
 await waitFor(()=>expect(api.download).toHaveBeenCalledExactlyOnceWith(path,filename))
})
test('F10-10.7-03 real SystemTab chargeback report displays user totals and date-range requests',async()=>{
 system();const cell=await screen.findByText('Synthetic student');const row=cell.closest('tr')!
 expect(within(row).getByText('4')).toBeInTheDocument();expect(within(row).getByText('2.5')).toBeInTheDocument();expect(within(row).getByText('125g')).toBeInTheDocument();expect(within(row).getByText('USD 7.25')).toBeInTheDocument()
 fireEvent.change(screen.getByLabelText('Start Date'),{target:{value:'2026-10-01'}});fireEvent.change(screen.getByLabelText('End Date'),{target:{value:'2026-10-12'}})
 await waitFor(()=>expect(api.report).toHaveBeenLastCalledWith('2026-10-01','2026-10-12'))
})
test('F10-10.9-04 SystemTab schedule creation serializes type/frequency and trimmed recipient list',async()=>{
 system();await screen.findByText('Synthetic weekly report');fireEvent.click(screen.getByRole('button',{name:'New Schedule'}))
 expect(screen.getByRole('button',{name:'Create Schedule'})).toBeDisabled()
 fireEvent.change(screen.getByLabelText('Name'),{target:{value:'Synthetic monthly'}})
 fireEvent.change(screen.getByLabelText('Report Type'),{target:{value:'chargeback_summary'}});fireEvent.change(screen.getByLabelText('Frequency'),{target:{value:'monthly'}})
 fireEvent.change(screen.getByLabelText('Recipients (comma-separated emails)'),{target:{value:' synthetic@example.invalid, , second@example.invalid '}})
 fireEvent.click(screen.getByRole('button',{name:'Create Schedule'}))
 await waitFor(()=>expect(api.create).toHaveBeenCalledExactlyOnceWith({name:'Synthetic monthly',report_type:'chargeback_summary',frequency:'monthly',recipients:['synthetic@example.invalid','second@example.invalid']}))
 await waitFor(()=>expect(screen.queryByRole('button',{name:'Create Schedule'})).not.toBeInTheDocument())
})
test('F10-10.9-04 schedule pause targets exact ID without sending any report',async()=>{
 system();await screen.findByText('Synthetic weekly report');fireEvent.click(screen.getByRole('button',{name:'Pause schedule'}))
 await waitFor(()=>expect(api.update).toHaveBeenCalledExactlyOnceWith(19,{is_active:false}))
 expect(api.run).not.toHaveBeenCalled();expect(api.remove).not.toHaveBeenCalled()
})

test('below Pro the chargeback report is hidden, analytics report types are not offered and their run-now is locked',async()=>{
 lic.features=[]
 api.list.mockResolvedValue([{id:21,name:'Synthetic failure report',report_type:'failure_analysis',frequency:'weekly',recipients:['synthetic@example.invalid'],is_active:true},{id:19,name:'Synthetic weekly report',report_type:'job_summary',frequency:'weekly',recipients:['synthetic@example.invalid'],is_active:true}])
 system();await screen.findByText('Synthetic failure report')
 expect(api.report).not.toHaveBeenCalled()
 expect(screen.getByRole('button',{name:'Run "Synthetic failure report" now'})).toBeDisabled()
 expect(screen.getByRole('button',{name:'Run "Synthetic weekly report" now'})).toBeEnabled()
 fireEvent.click(screen.getByRole('button',{name:'New Schedule'}))
 const options=[...(screen.getByLabelText('Report Type') as HTMLSelectElement).options].map(o=>o.value)
 expect(options).toEqual(['job_summary','filament_consumption'])
})
