import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import Cameras from './Cameras'
import CameraPipProvider from '../../components/printers/CameraPipProvider'
vi.mock('../../hooks/useWebRTC', () => ({ default: () => ({videoRef:{current:null},status:'connecting',retry:vi.fn()}) }))
const feeds = [{id:11,name:'Synthetic Alpha',camera_enabled:true},{id:12,name:'Synthetic Beta',camera_enabled:true},{id:13,name:'Synthetic Gamma',camera_enabled:true}]
let failList = false
function mount() {
 const client = new QueryClient({defaultOptions:{queries:{retry:false}}})
 client.setQueryData(['auth','me'],{id:3,role:'admin',group_id:1})
 return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/cameras']}><CameraPipProvider><Cameras /></CameraPipProvider></MemoryRouter></QueryClientProvider>)
}
beforeEach(() => {
 failList = false
 vi.stubGlobal('fetch',vi.fn(async (url:unknown) => ({ok: !(failList && String(url)==='/api/cameras'),status:503,json:async () => String(url)==='/api/cameras' ? feeds : {enabled:false}})))
})
afterEach(() => {cleanup();vi.unstubAllGlobals()})
test('F02-2.1-02 all returned active feeds render as videos and filtering selects named feed', async () => {
 const view = mount()
 for (const camera of feeds) expect(await screen.findByRole('link',{name:camera.name})).toHaveAttribute('href',`/cameras/${camera.id}`)
 expect(view.container.querySelectorAll('video')).toHaveLength(3)
 expect(screen.getAllByText('Connecting...')).toHaveLength(3)
 expect(screen.queryByText('LIVE')).not.toBeInTheDocument()
 fireEvent.change(screen.getByPlaceholderText('Filter cameras...'),{target:{value:'bETA'}})
 expect(view.container.querySelectorAll('video')).toHaveLength(1)
 expect(screen.getByRole('link',{name:'Synthetic Beta'})).toBeInTheDocument()
 expect(screen.queryByRole('link',{name:'Synthetic Alpha'})).not.toBeInTheDocument()
 fireEvent.change(screen.getByPlaceholderText('Filter cameras...'),{target:{value:''}})
 expect(view.container.querySelectorAll('video')).toHaveLength(3)
})
test('F02-2.3-01/02 F opens multi-feed viewport overlay, Escape removes it and root mode', async () => {
 const view = mount()
 await screen.findByRole('link',{name:'Synthetic Alpha'})
 fireEvent.keyDown(window,{key:'f'})
 const exit=screen.getByRole('button',{name:'Exit (Esc)'})
 const overlay=exit.closest('.fixed')!
 expect(overlay).toHaveClass('inset-0')
 expect(overlay.querySelectorAll('video')).toHaveLength(3)
 for(const camera of feeds) expect(overlay.textContent).toContain(camera.name)
 expect(document.documentElement).toHaveClass('control-room-mode')
 fireEvent.keyDown(window,{key:'Escape'})
 expect(screen.queryByRole('button',{name:'Exit (Esc)'})).not.toBeInTheDocument()
 expect(view.container.querySelectorAll('video')).toHaveLength(3)
 expect(document.documentElement).not.toHaveClass('control-room-mode')
})
test('F02-2.6-02 TV action opens dedicated route in new tab with opener protection',async()=>{
 mount(); await screen.findByRole('link',{name:'Synthetic Alpha'})
 const link=screen.getByTitle('Open TV Dashboard in new tab')
 expect(link).toHaveAttribute('href','/tv')
 expect(link).toHaveAttribute('target','_blank')
 expect(link).toHaveAttribute('rel','noopener noreferrer')
})
test('F02-2.1-02 failed camera list shows retry and never presents failure as empty configuration',async()=>{
 failList=true;mount()
 expect(await screen.findByRole('alert')).toHaveTextContent('Unable to load cameras')
 expect(screen.queryByText('No cameras configured')).not.toBeInTheDocument()
 failList=false
 fireEvent.click(screen.getByRole('button',{name:'Retry cameras'}))
 await screen.findByRole('link',{name:'Synthetic Alpha'})
 await waitFor(()=>expect(screen.queryByRole('alert')).not.toBeInTheDocument())
})
