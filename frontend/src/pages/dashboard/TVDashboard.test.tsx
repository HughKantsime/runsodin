import { act, render, screen, fireEvent, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import TVDashboard from './TVDashboard'
const state=vi.hoisted(()=>({printers:[] as any[],alerts:{print_failed:0,spool_low:0,maintenance_overdue:0,total:0},navigate:vi.fn(),printerError:false,alertError:false,loading:false,alertLoading:false,retry:vi.fn()}))
vi.mock('react-router-dom',()=>({useNavigate:()=>state.navigate}))
vi.mock('@tanstack/react-query',()=>({useQuery:({queryKey}:any)=>({data:queryKey[0]==='printers'?state.printers:state.alerts,isLoading:queryKey[0]==='printers'?state.loading:state.alertLoading,isError:queryKey[0]==='printers'?state.printerError:state.alertError,refetch:state.retry})}))
vi.mock('../../BrandingContext',()=>({useBranding:()=>({app_name:'Test farm'})}))
vi.mock('../../hooks/useWebRTC',()=>({default:()=>({videoRef:{current:null},status:'connecting'})}))
const printer=(id:number,extra={})=>({id,name:`Printer ${id}`,online:true,gcode_state:'IDLE',...extra})
beforeEach(()=>{vi.useFakeTimers();vi.setSystemTime(new Date('2026-10-11T12:00:00Z'));state.printers=[];state.alerts={print_failed:0,spool_low:0,maintenance_overdue:0,total:0};state.navigate.mockReset();state.retry.mockReset();state.printerError=false;state.alertError=false;state.loading=false;state.alertLoading=false})
afterEach(()=>{cleanup();vi.useRealTimers()})
test('fleet totals include paused jobs and errors outside the visible page',()=>{
 state.printers=Array.from({length:13},(_,i)=>printer(i+1));state.printers[0].gcode_state='RUNNING';state.printers[12]={...state.printers[12],gcode_state:'PAUSE',hms_errors:[{code:'Synthetic HMS'}]};state.alerts={print_failed:1,spool_low:1,maintenance_overdue:0,total:2};render(<TVDashboard/>);
 expect(screen.getByText('2/13 Printing')).toBeInTheDocument();expect(screen.getByText('1 Error')).toBeInTheDocument();expect(screen.getByText('2 Active Alerts')).toBeInTheDocument();expect(screen.getAllByTestId('tv-printer-card')).toHaveLength(12)
})
test('rotates twelve-card pages every thirty seconds, wraps and clears timers on exit',()=>{
 state.printers=Array.from({length:13},(_,i)=>printer(i+1));const view=render(<TVDashboard/>);
 act(()=>vi.advanceTimersByTime(29999));expect(screen.getAllByTestId('tv-printer-card')).toHaveLength(12);
 act(()=>vi.advanceTimersByTime(1));expect(screen.getAllByTestId('tv-printer-card')).toHaveLength(1);expect(screen.getByText('Printer 13')).toBeInTheDocument();
 act(()=>vi.advanceTimersByTime(30000));expect(screen.getAllByTestId('tv-printer-card')).toHaveLength(12);
 view.unmount();expect(vi.getTimerCount()).toBe(0);fireEvent.keyDown(window,{key:'Escape'});expect(state.navigate).not.toHaveBeenCalled()
})
test('shrinking a fleet resets an out-of-range page',()=>{
 state.printers=Array.from({length:13},(_,i)=>printer(i+1));const view=render(<TVDashboard/>);act(()=>vi.advanceTimersByTime(30000));state.printers=[printer(1)];view.rerender(<TVDashboard/>);expect(screen.getByText('Printer 1')).toBeInTheDocument();expect(screen.getAllByTestId('tv-printer-card')).toHaveLength(1)
})
test('renders printing progress, job and ETA while disabled cameras have no video',()=>{
 state.printers=[printer(1,{gcode_state:'RUNNING',mc_percent:45,mc_remaining_time:75,gcode_file:'Test job',camera_url:'synthetic',camera_enabled:false})];render(<TVDashboard/>);expect(screen.getByTestId('tv-printer-status')).toHaveTextContent('Printing');expect(screen.getByTestId('tv-printer-progress')).toHaveTextContent('45%');expect(screen.getByTestId('tv-printer-job')).toHaveTextContent('Test job');expect(screen.getByText('ETA 1h 15m')).toBeInTheDocument();expect(document.querySelector('video')).toBeNull()
})
test('exit button and Escape navigate back to dashboard',()=>{
 render(<TVDashboard/>);fireEvent.click(screen.getByTitle('Exit TV Mode (Esc)'));fireEvent.keyDown(window,{key:'Escape'});expect(state.navigate.mock.calls).toEqual([['/'],['/']]);expect(screen.getByText('No active alerts')).toBeInTheDocument()
})

test('printer failure is explicit, offers retry and does not invent empty fleet totals',()=>{
 state.printerError=true;render(<TVDashboard/>);expect(screen.getByRole('alert')).toHaveTextContent('Printer data unavailable');expect(screen.queryByText('0/0 Printing')).not.toBeInTheDocument();fireEvent.click(screen.getByRole('button',{name:'Retry printer data'}));expect(state.retry).toHaveBeenCalledOnce()
})
test('alert failure never claims no active alerts',()=>{
 state.alertError=true;render(<TVDashboard/>);expect(screen.getByText('Alert status unavailable')).toBeInTheDocument();expect(screen.queryByText('No active alerts')).not.toBeInTheDocument()
})
test('initial loading exposes status and does not assert a zero fleet',()=>{
 state.loading=true;render(<TVDashboard/>);expect(screen.getByRole('status')).toHaveTextContent('Loading printers');expect(screen.queryByText('0/0 Printing')).not.toBeInTheDocument()
})

test('loading alert state does not assert no active alerts',()=>{
 state.alertLoading=true;render(<TVDashboard/>);expect(screen.getByText('Loading alerts')).toBeInTheDocument();expect(screen.queryByText('No active alerts')).not.toBeInTheDocument()
})
test('failed printer refresh suppresses cached fleet totals and cards',()=>{
 state.printers=[printer(1,{gcode_state:'RUNNING',hms_errors:[{code:'Synthetic HMS'}]})];state.printerError=true;render(<TVDashboard/>);expect(screen.queryByText('1/1 Printing')).not.toBeInTheDocument();expect(screen.queryByText('1 Error')).not.toBeInTheDocument();expect(screen.queryByTestId('tv-printer-card')).not.toBeInTheDocument();expect(screen.getByRole('alert')).toHaveTextContent('Printer data unavailable')
})
