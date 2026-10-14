import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import FailureReasonModal from './FailureReasonModal'
import RecentlyCompleted from './RecentlyCompleted'
import JobRow from './JobRow'
import { EditJobModal } from './JobModals'
vi.mock('../../permissions',()=>({canDo:()=>false}))
const job={id:51,item_name:'Synthetic job',status:'pending',quantity:1,hold:false,is_locked:false,priority:3,colors_list:[],due_date:'2026-10-09T12:00:00Z'}
beforeEach(()=>{vi.useFakeTimers({toFake:['Date']});vi.setSystemTime(new Date('2026-10-12T12:00:00Z'))})
afterEach(()=>{cleanup();vi.useRealTimers();vi.restoreAllMocks()})
function row(overrides={}){return render(<table><tbody><JobRow job={{...job,...overrides}} onAction={vi.fn()} isSelected={false} onToggleSelect={vi.fn()}/></tbody></table>)}
test.each(['clog','adhesion','filament_tangle'])('F04-4.5-01 failure %s and free notes submit for exact job',async reason=>{
 const submit=vi.fn().mockResolvedValue(undefined),close=vi.fn()
 render(<FailureReasonModal jobId={51} jobName="Synthetic job" onSubmit={submit} onClose={close}/>)
 expect(screen.getByRole('button',{name:'Mark as Failed'})).toBeDisabled()
 fireEvent.change(screen.getByRole('combobox',{name:/Failure Reason/}),{target:{value:reason}})
 fireEvent.change(screen.getByRole('textbox',{name:/Notes/}),{target:{value:'Synthetic failure notes'}})
 fireEvent.click(screen.getByRole('button',{name:'Mark as Failed'}))
 await waitFor(()=>expect(submit).toHaveBeenCalledExactlyOnceWith(51,reason,'Synthetic failure notes'))
 await waitFor(()=>expect(close).toHaveBeenCalledOnce())
})
test('F04-4.5-01 cancel preserves existing reason and never submits',()=>{
 const submit=vi.fn(),close=vi.fn()
 render(<FailureReasonModal jobId={51} existingReason="clog" existingNotes="Synthetic saved" onSubmit={submit} onClose={close}/>)
 expect(screen.getByRole('combobox',{name:/Failure Reason/})).toHaveValue('clog')
 expect(screen.getByRole('textbox',{name:/Notes/})).toHaveValue('Synthetic saved')
 fireEvent.click(screen.getByRole('button',{name:'Cancel'}))
 expect(submit).not.toHaveBeenCalled();expect(close).toHaveBeenCalledOnce()
})
test('F04-4.2-09 edit submits numeric priority and due date; badges have priority styling',()=>{
 const submit=vi.fn()
 const view=render(<EditJobModal isOpen job={job} onSubmit={submit} onClose={vi.fn()}/>)
 const priority=screen.getAllByRole('combobox').find(e=>Array.from(e.querySelectorAll('option')).some(o=>o.textContent==='2 - High'))!
 fireEvent.change(priority,{target:{value:'2'}})
 fireEvent.change(view.container.querySelector('input[type=date]')!,{target:{value:'2026-10-20'}})
 fireEvent.click(screen.getByRole('button',{name:/Save Changes/}))
 expect(submit).toHaveBeenCalledWith(51,expect.objectContaining({priority:2,due_date:'2026-10-20'}))
 view.unmount();row({priority:2})
 expect(screen.getByText('P2')).toHaveClass('text-red-400')
 expect(screen.getByText('3d overdue')).toBeInTheDocument()
})
test('F04-4.3-03 overdue pending job row declares red warning tint',()=>{
 row();expect(screen.getByRole('row')).toHaveClass('bg-red-950/30','border-l-red-500')
})
test.each(['completed','cancelled','failed'])('F04-4.3-03 overdue %s terminal job is not tinted as at risk',status=>{
 row({status});expect(screen.getByRole('row')).not.toHaveClass('bg-red-950/30')
})
const recentJobs=Array.from({length:10},(_,i)=>({id:i,item_name:`Synthetic recent ${i}`,status:i%2?'failed':'completed',actual_end:`2026-10-${String(i+1).padStart(2,'0')}T12:00:00Z`,printer_name:'Synthetic printer',model_name:null,duration_seconds:7200,fail_reason:i%2?'nozzle_clog':null,thumbnail_data:'data:image/png;base64,iVBORw0KGgo='})).reverse().slice(0,8)
test('F04-4.9-01 server-projected newest eight outcomes render actual durations/failure reasons',()=>{
 render(<RecentlyCompleted jobs={recentJobs}/>)
 const names=screen.getAllByText(/^Synthetic recent/).map(n=>n.textContent)
 expect(names).toEqual([9,8,7,6,5,4,3,2].map(i=>`Synthetic recent ${i}`))
 expect(screen.queryByText('Synthetic pending')).not.toBeInTheDocument()
 expect(screen.getAllByText('Synthetic printer · 2h')).toHaveLength(8)
 expect(screen.getAllByText('nozzle clog')).toHaveLength(4)
})
test('F04-4.9-01 recent print cards display authorized data thumbnails',()=>{
 const view=render(<RecentlyCompleted jobs={recentJobs}/>)
 expect(view.container.querySelectorAll('img')).toHaveLength(8)
})
