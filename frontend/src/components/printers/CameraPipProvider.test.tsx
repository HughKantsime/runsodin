import { beforeEach, afterEach, expect, test, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Routes, Route, Link } from 'react-router-dom'
import OrgContext from '../../contexts/OrgContext'
import CameraPipProvider, { useCameraPip } from './CameraPipProvider'
const peer=vi.hoisted(()=>({status:'connecting',retry:vi.fn(),closed:[] as number[]}))
vi.mock('../../hooks/useWebRTC',async()=>{
 const React=await import('react')
 return {default:(id:number)=>{const ref=React.useRef(null);React.useEffect(()=>()=>{peer.closed.push(id)},[id]);return {videoRef:ref,status:peer.status,retry:peer.retry}}}
})
const camera={id:9,name:'Synthetic camera',camera_enabled:true,has_camera:true,display_order:0}
let client:QueryClient;let cameras:any[];let failure:number
function Trigger(){const {open}=useCameraPip();return <button onClick={()=>open(camera)}>Open player</button>}
function Harness({orgId=null,protectedPage=true}:{orgId?:number|null;protectedPage?:boolean}){
 return <QueryClientProvider client={client}><OrgContext.Provider value={{orgId,orgName:null,isAdmin:false,orgs:[],loading:false,switchOrg:()=>{}} as any}><MemoryRouter>{protectedPage?<CameraPipProvider><Trigger/><Link to="/jobs">Jobs</Link><Routes><Route path="/" element={<h1>Cameras route</h1>}/><Route path="/jobs" element={<h1>Jobs route</h1>}/></Routes></CameraPipProvider>:<h1>Login route</h1>}</MemoryRouter></OrgContext.Provider></QueryClientProvider>
}
async function open(){await waitFor(()=>expect(client.getQueriesData({queryKey:['cameras']}).some(([,data])=>Array.isArray(data)&&data.length>0)).toBe(true));fireEvent.click(screen.getByRole('button',{name:'Open player'}));await screen.findByRole('region',{name:'Picture-in-picture camera'})}
async function refresh(){await act(async()=>{await client.invalidateQueries({queryKey:['cameras']})})}
beforeEach(()=>{
 cameras=[camera];failure=0;peer.status='connecting';peer.retry.mockReset();peer.closed=[]
 client=new QueryClient({defaultOptions:{queries:{retry:false}}});client.setQueryData(['auth','me'],{id:3,group_id:1,role:'admin'})
 vi.stubGlobal('fetch',vi.fn(async()=>({ok:!failure,status:failure||200,json:async()=>cameras})))
})
afterEach(()=>{client.clear();vi.unstubAllGlobals()})
test('one player survives route navigation and closes explicitly',async()=>{
 render(<Harness/>);await open();fireEvent.click(screen.getByRole('link',{name:'Jobs'}));await screen.findByRole('heading',{name:'Jobs route'})
 expect(screen.getAllByRole('region',{name:'Picture-in-picture camera'})).toHaveLength(1);expect(peer.closed).toEqual([])
 fireEvent.click(screen.getByRole('button',{name:'Close camera player'}));expect(screen.queryByRole('region')).not.toBeInTheDocument();expect(peer.closed).toEqual([9])
})
test('public branch unmounts media',async()=>{const view=render(<Harness/>);await open();view.rerender(<Harness protectedPage={false}/>);expect(screen.queryByRole('region')).not.toBeInTheDocument();expect(peer.closed).toEqual([9])})
test.each([{id:4,group_id:1,role:'admin'},{id:3,group_id:2,role:'admin'},{id:3,group_id:1,role:'viewer'}])('verified principal change closes media %j',async user=>{
 render(<Harness/>);await open();await act(async()=>{client.setQueryData(['auth','me'],user)});await waitFor(()=>expect(screen.queryByRole('region')).not.toBeInTheDocument());expect(peer.closed).toEqual([9])
})
test('selected organization change closes media',async()=>{const view=render(<Harness/>);await open();view.rerender(<Harness orgId={2}/>);expect(screen.queryByRole('region')).not.toBeInTheDocument();expect(peer.closed).toEqual([9])})
test.each([{list:[]},{list:[{...camera,camera_enabled:false}]}])('fresh removal or disable closes media',async ({list})=>{render(<Harness/>);await open();cameras=list;await refresh();await waitFor(()=>expect(screen.queryByRole('region')).not.toBeInTheDocument());expect(peer.closed).toEqual([9])})
test.each([401,403])('authorization failure %i closes media',async status=>{render(<Harness/>);await open();failure=status;await refresh();await waitFor(()=>expect(screen.queryByRole('region')).not.toBeInTheDocument());expect(peer.closed).toEqual([9])})
test('network failure retains player with explicit uncertainty',async()=>{render(<Harness/>);await open();failure=503;await refresh();expect(screen.getByRole('region')).toBeInTheDocument();expect(await screen.findByText('Camera list unavailable. Access changes have not been checked.')).toBeInTheDocument();expect(peer.closed).toEqual([])})
test('error status offers retry and never labels media live',async()=>{peer.status='error';render(<Harness/>);await open();expect(screen.queryByText('LIVE')).not.toBeInTheDocument();fireEvent.click(screen.getByRole('button',{name:'Retry stream'}));expect(peer.retry).toHaveBeenCalledTimes(1)})
test('only live hook status displays LIVE',async()=>{peer.status='live';render(<Harness/>);await open();expect(screen.getByText('LIVE')).toBeInTheDocument()})
test('unknown identity cannot reuse previous camera cache',async()=>{client.setQueryData(['auth','me'],{username:'synthetic',role:'admin'});client.setQueryData(['cameras'],[camera]);render(<Harness/>);fireEvent.click(screen.getByRole('button',{name:'Open player'}));expect(screen.queryByRole('region')).not.toBeInTheDocument();expect(fetch).not.toHaveBeenCalled()})
test('resize and viewport shrink stay within screen',async()=>{
 render(<Harness/>);await open();fireEvent.click(screen.getByRole('button',{name:'Resize camera player'}));vi.stubGlobal('innerWidth',180);vi.stubGlobal('innerHeight',120);fireEvent(window,new Event('resize'))
 const player=screen.getByRole('region');expect(player.style.width).toBe('180px');expect(player.style.height).toBe('120px');expect(player.style.left).toBe('0px');expect(player.style.top).toBe('0px')
})
test('pointer drag clamps, captures and stops on release or cancellation',async()=>{
 class Pointer extends MouseEvent {pointerId:number;constructor(type:string,options:any){super(type,options);this.pointerId=options.pointerId??1}}
 vi.stubGlobal('PointerEvent',Pointer)
 render(<Harness/>);await open();const player=screen.getByRole('region');const header=player.querySelector('header')! as any
 header.setPointerCapture=vi.fn();header.releasePointerCapture=vi.fn()
 fireEvent.pointerDown(header,{pointerId:1,button:0,clientX:20,clientY:20});expect(header.setPointerCapture).toHaveBeenCalledWith(1)
 fireEvent.pointerMove(header,{pointerId:1,clientX:9999,clientY:9999});expect(player.style.left).toBe(`${innerWidth-320}px`);expect(player.style.top).toBe(`${innerHeight-220}px`)
 fireEvent.pointerUp(header,{pointerId:1});expect(header.releasePointerCapture).toHaveBeenCalledWith(1)
 fireEvent.pointerMove(header,{pointerId:1,clientX:0,clientY:0});expect(player.style.left).toBe(`${innerWidth-320}px`)
 fireEvent.pointerDown(header,{pointerId:2,button:0,clientX:20,clientY:20});fireEvent.pointerCancel(header,{pointerId:2});fireEvent.pointerMove(header,{pointerId:2,clientX:0,clientY:0});expect(player.style.left).toBe(`${innerWidth-320}px`)
})
test('player removes its resize listener on close',async()=>{
 const add=vi.spyOn(window,'addEventListener');const remove=vi.spyOn(window,'removeEventListener')
 render(<Harness/>);await open();const listener=add.mock.calls.find(([event])=>event==='resize')![1]
 fireEvent.click(screen.getByRole('button',{name:'Close camera player'}));expect(remove).toHaveBeenCalledWith('resize',listener)
 add.mockRestore();remove.mockRestore()
})
