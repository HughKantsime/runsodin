import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import Timeline from '../pages/jobs/Timeline'
import PrintLog from '../pages/analytics/PrintLog'
import DetailDrawer from '../components/shared/DetailDrawer'
import Modal from '../components/ui/Modal'
const api=vi.hoisted(()=>({timeline:vi.fn(),job:vi.fn(),export:vi.fn()}))
vi.mock('../api',()=>({timeline:{get:api.timeline},printers:{list:vi.fn().mockResolvedValue([{id:9,name:'Synthetic printer'}])},printJobs:{get:api.job},archives:{log:vi.fn().mockResolvedValue({items:[],total:0}),logExport:api.export},jobs:{},jobActions:{}}))
beforeEach(()=>{
 const now=new Date(),start=new Date(now);start.setHours(10,0,0,0);const end=new Date(start.getTime()+5400000)
 api.timeline.mockResolvedValue({slots:[{mqtt_job_id:71,printer_id:9,printer_name:'Synthetic printer',item_name:'Synthetic timed print',status:'completed',start:start.toISOString(),end:end.toISOString()}]})
 api.job.mockResolvedValue({filename:'Synthetic timed print',status:'complete',total_duration:5400,remaining_time:600,current_layer:20,total_layers:100})
 api.export.mockResolvedValue('id,actual_duration_seconds\r\n71,5400\r\n')
})
afterEach(()=>{cleanup();vi.clearAllMocks();vi.unstubAllGlobals();vi.restoreAllMocks()})
function mount(child:React.ReactNode){const client=new QueryClient({defaultOptions:{queries:{retry:false}}});return render(<QueryClientProvider client={client}><MemoryRouter>{child}</MemoryRouter></QueryClientProvider>)}
test('F04-4.7-02 actual timeline print-job detail renders known tracked duration and remaining time',async()=>{
 mount(<Timeline/>);await screen.findAllByText('Synthetic timed print')
 fireEvent.click(screen.getAllByText('Synthetic timed print')[0])
 await screen.findByText('Total duration')
 expect(screen.getByText('Total duration').parentElement).toHaveTextContent('1h 30m')
 expect(screen.getByText('Remaining').parentElement).toHaveTextContent('10m')
 expect(api.job).toHaveBeenCalledWith(71)
})
test('F04-4.7-02 PrintLog export preserves returned actual-duration CSV column and bytes',async()=>{
 let blob:Blob|undefined
 vi.stubGlobal('URL',class extends URL {static createObjectURL(value:Blob){blob=value;return 'blob:synthetic-time-export'};static revokeObjectURL=vi.fn()})
 const click=vi.spyOn(HTMLAnchorElement.prototype,'click').mockImplementation(()=>{})
 mount(<PrintLog/>);fireEvent.click(screen.getByRole('button',{name:'Export CSV'}))
 await waitFor(()=>expect(click).toHaveBeenCalledOnce())
 const contents=await new Promise<string>((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result));reader.onerror=reject;reader.readAsText(blob!)})
 expect(contents).toBe('id,actual_duration_seconds\r\n71,5400\r\n')
 expect(URL.revokeObjectURL).toHaveBeenCalledExactlyOnceWith('blob:synthetic-time-export')
})
test('F12-12.7-03 actual Modal icon-only close is named and invokes closure',()=>{
 const close=vi.fn();render(<Modal isOpen title='Synthetic named close' onClose={close}><p>Body</p></Modal>)
 fireEvent.click(screen.getByRole('button',{name:'Close',exact:true}));expect(close).toHaveBeenCalledOnce()
})
test('F12-12.7-03 actual job DetailDrawer icon-only close has accessible name',()=>{
 render(<DetailDrawer open title='Synthetic job drawer' onClose={vi.fn()}><p>Body</p></DetailDrawer>)
 expect(screen.queryByRole('button',{name:/close/i})).toBeInTheDocument()
})
