import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import Detections from './Detections'
const api=vi.hoisted(()=>({get:vi.fn(),review:vi.fn(),bulk:vi.fn(),allowed:true,error:vi.fn()}))
vi.mock('../../api',()=>({auth:{me:vi.fn()},vision:{getDetections:api.get,getStats:vi.fn().mockResolvedValue({days:7,by_type:{spaghetti:1}}),reviewDetection:api.review,bulkReview:api.bulk},printers:{list:vi.fn().mockResolvedValue([{id:9,name:'Synthetic printer'}])}}))
vi.mock('react-hot-toast',()=>({default:{error:api.error,success:vi.fn()}}))
vi.mock('../../permissions',()=>({canDo:(action: string)=>action === "printers.edit" && api.allowed}))
vi.mock('../../components/vision/BoundedVisionImage',()=>({default:({path,alt,className,onClick,onImage}:any)=><img src={'/api/vision/frames/'+path} alt={alt} className={className} onClick={onClick} onLoad={event=>onImage?.({width:event.currentTarget.naturalWidth,height:event.currentTarget.naturalHeight,url:event.currentTarget.src,digest:'renderer-fixture'})}/>}))
const detection={id:17,printer_id:9,printer_name:'Synthetic printer',detection_type:'spaghetti',status:'pending',confidence:.87,frame_path:'synthetic.jpg',bbox_json:'[10,20,110,120]',created_at:'2026-10-12T12:00:00Z'}
beforeEach(()=>{api.allowed=true;api.get.mockResolvedValue({items:[detection],total:61});api.review.mockResolvedValue({})})
afterEach(()=>{cleanup();vi.clearAllMocks()})
function mount(){const client=new QueryClient({defaultOptions:{queries:{retry:false}}});client.setQueryData(['auth','me'],{id:3,username:'Synthetic admin',role:'admin',group_id:null});return render(<QueryClientProvider client={client}><MemoryRouter><Detections/></MemoryRouter></QueryClientProvider>)}
async function loaded(){const view=mount();await screen.findByRole('img',{name:'Detection thumbnail'});return view}
test('F03-3.2-01 grid renders returned detection frame/confidence and next page requests offset',async()=>{
 await loaded()
 expect(screen.getByRole('img',{name:'Detection thumbnail'})).toHaveAttribute('src','/api/vision/frames/synthetic.jpg')
 expect(screen.getByText('87%')).toBeInTheDocument()
 expect(screen.getByRole('link',{name:'Synthetic printer'})).toHaveAttribute('href','/cameras/9')
 fireEvent.click(screen.getByRole('button',{name:'Next'}))
 await waitFor(()=>expect(api.get).toHaveBeenLastCalledWith({limit:30,offset:30}))
 expect(await screen.findByText('Page 2 of 3')).toBeInTheDocument()
})
test.each(['pending','confirmed','dismissed','auto_paused'])('F03-3.2-02 %s status filter combines with type/printer and resets pagination',async status=>{
 await loaded();fireEvent.click(screen.getByRole('button',{name:'Next'}))
 await waitFor(()=>expect(api.get).toHaveBeenLastCalledWith({limit:30,offset:30}))
 const selects=screen.getAllByRole('combobox')
 fireEvent.change(selects[0],{target:{value:'detachment'}})
 fireEvent.change(selects[1],{target:{value:status}})
 await screen.findByRole('option',{name:'Synthetic printer'})
 fireEvent.change(selects[2],{target:{value:'9'}})
 await waitFor(()=>expect(api.get).toHaveBeenLastCalledWith({limit:30,offset:0,detection_type:'detachment',status,printer_id:'9'}))
 expect(screen.getByText('Page 1 of 3')).toBeInTheDocument()
})
test('F03-3.2-03 frame viewer overlays bbox using loaded image natural coordinates',async()=>{
 const view=await loaded();fireEvent.click(screen.getByRole('img',{name:'Detection thumbnail'}))
 const image=screen.getByRole('img',{name:'Detection frame'})
 expect(image).toHaveAttribute('src','/api/vision/frames/synthetic.jpg')
 Object.defineProperty(image,'naturalWidth',{configurable:true,value:640});Object.defineProperty(image,'naturalHeight',{configurable:true,value:480})
 fireEvent.load(image)
 const rect=view.container.querySelector('svg rect')!
 expect(rect).toHaveAttribute('x','10');expect(rect).toHaveAttribute('y','20');expect(rect).toHaveAttribute('width','100');expect(rect).toHaveAttribute('height','100')
 expect(rect.parentElement).toHaveAttribute('viewBox','0 0 640 480')
})
test.each([['Confirm Issue','confirmed'],['Dismiss','dismissed']])('F03-3.2-04 frame %s reviews exact detection and closes viewer',async(label,status)=>{
 await loaded();fireEvent.click(screen.getByRole('img',{name:'Detection thumbnail'}))
 // Dismiss also appears as an inline quick action; choose the viewer action.
 const buttons=screen.getAllByRole('button',{name:label,exact:true})
 fireEvent.click(buttons[buttons.length-1])
 await waitFor(()=>expect(api.review).toHaveBeenCalledExactlyOnceWith(17,status))
 await waitFor(()=>expect(screen.queryByRole('img',{name:'Detection frame'})).not.toBeInTheDocument())
})
test('F03-3.2-04 denied review permission hides review actions while retaining viewer',async()=>{
 api.allowed=false;await loaded();fireEvent.click(screen.getByRole('img',{name:'Detection thumbnail'}))
 expect(screen.getByRole('img',{name:'Detection frame'})).toBeInTheDocument()
 expect(screen.queryByRole('button',{name:'Confirm Issue'})).not.toBeInTheDocument()
 expect(screen.queryByRole('button',{name:'Dismiss',exact:true})).not.toBeInTheDocument()
 expect(api.review).not.toHaveBeenCalled()
})

test('F03-3.2-04 frame review exposes advertised training-label action',async()=>{
 await loaded();fireEvent.click(screen.getByRole('img',{name:'Detection thumbnail'}))
 expect(screen.queryByRole('button',{name:/label.*training|training.*label|label detection/i})).toBeInTheDocument()
})

test('F03-3.2-01 detections exposes advertised list-view switch',async()=>{
 await loaded()
 expect(screen.queryByRole('button',{name:/list view|view as list|show list/i})).toBeInTheDocument()
})

test('an older selection review failure cannot toast over a newly opened record', async()=>{
 let reject!: (error: Error) => void
 api.get.mockResolvedValue({items:[detection,{...detection,id:18,frame_path:'second.jpg'}],total:2})
 api.review.mockImplementationOnce(()=>new Promise((_resolve,fail)=>{reject=fail}))
 mount(); await screen.findAllByRole('img',{name:'Detection thumbnail'}); fireEvent.click(screen.getAllByRole('img',{name:'Detection thumbnail'})[0])
 fireEvent.click(screen.getByRole('button',{name:'Confirm Issue'}))
 await waitFor(()=>expect(api.review).toHaveBeenCalledWith(17,'confirmed'))
 fireEvent.click(screen.getAllByRole('img',{name:'Detection thumbnail'})[1])
 reject(new Error('old failure'))
 await waitFor(()=>expect(screen.getByRole('img',{name:'Detection frame'})).toHaveAttribute('src','/api/vision/frames/second.jpg'))
 await new Promise(resolve=>setTimeout(resolve,0))
 expect(api.error).not.toHaveBeenCalled()
})
