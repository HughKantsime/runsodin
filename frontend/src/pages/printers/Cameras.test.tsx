import { beforeEach, afterEach, expect, test, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Routes, Route, Link } from 'react-router-dom'
import Cameras from './Cameras'
import CameraPipProvider from '../../components/printers/CameraPipProvider'
vi.mock('../../hooks/useWebRTC',()=>({default:()=>({videoRef:{current:null},status:'connecting',retry:vi.fn()})}))
const camera={id:9,name:'Synthetic camera',camera_enabled:true}
function Harness(){
 const client=new QueryClient({defaultOptions:{queries:{retry:false}}});client.setQueryData(['auth','me'],{id:3,role:'admin',group_id:1})
 return <QueryClientProvider client={client}><MemoryRouter initialEntries={['/cameras']}><CameraPipProvider><Link to="/other">Other page</Link><div contentEditable data-testid="editor"/><select aria-label="Synthetic select"><option>Option</option></select><Routes><Route path="/cameras" element={<Cameras/>}/><Route path="/other" element={<h1>Other route</h1>}/></Routes></CameraPipProvider></MemoryRouter></QueryClientProvider>
}
beforeEach(()=>{vi.stubGlobal('fetch',vi.fn(async(url:any)=>({ok:true,json:async()=>String(url).endsWith('/cameras')?[camera]:String(url).includes('/vision')?{enabled:false}:[{...camera,camera_url:'synthetic'}]})))})
afterEach(()=>vi.unstubAllGlobals())
test('advertised F shortcut opens control room and Escape exits',async()=>{
 render(<Harness/>);await screen.findByRole('link',{name:'Synthetic camera'});fireEvent.keyDown(window,{key:'f'})
 expect(screen.getByRole('button',{name:'Exit (Esc)'})).toBeInTheDocument();expect(document.documentElement).toHaveClass('control-room-mode')
 fireEvent.keyDown(window,{key:'Escape'});expect(screen.queryByRole('button',{name:'Exit (Esc)'})).not.toBeInTheDocument();expect(document.documentElement).not.toHaveClass('control-room-mode')
})
test('shortcut ignores editable fields and modifiers',async()=>{
 render(<Harness/>);await screen.findByRole('link',{name:'Synthetic camera'})
 for(const target of [screen.getByPlaceholderText('Filter cameras...'),screen.getByTestId('editor'),screen.getByRole('combobox')])fireEvent.keyDown(target,{key:'F',shiftKey:true})
 fireEvent.keyDown(window,{key:'f',ctrlKey:true});fireEvent.keyDown(window,{key:'f',metaKey:true});fireEvent.keyDown(window,{key:'f',altKey:true})
 expect(screen.queryByRole('button',{name:'Exit (Esc)'})).not.toBeInTheDocument()
})
test('picture in picture renders streaming video and honest connecting status',async()=>{
 const {container}=render(<Harness/>);await screen.findByRole('link',{name:'Synthetic camera'});fireEvent.click(screen.getByTitle('Picture-in-Picture'))
 expect(container.querySelectorAll('video')).toHaveLength(2);expect(screen.queryByRole('img',{name:'Camera stream'})).not.toBeInTheDocument()
 expect(screen.queryByText('LIVE')).not.toBeInTheDocument()
})
test('picture in picture remains while navigating to another protected page',async()=>{
 render(<Harness/>);await screen.findByRole('link',{name:'Synthetic camera'});fireEvent.click(screen.getByTitle('Picture-in-Picture'));fireEvent.click(screen.getByRole('link',{name:'Other page'}))
 await screen.findByRole('heading',{name:'Other route'});expect(screen.getByText('Synthetic camera')).toBeInTheDocument()
})
test('shortcut supports Shift+F and does not consume an already handled key',async()=>{
 render(<Harness/>);await screen.findByRole('link',{name:'Synthetic camera'})
 const handled=new KeyboardEvent('keydown',{key:'f',cancelable:true});handled.preventDefault();window.dispatchEvent(handled)
 expect(screen.queryByRole('button',{name:'Exit (Esc)'})).not.toBeInTheDocument()
 fireEvent.keyDown(window,{key:'F',shiftKey:true});expect(screen.getByRole('button',{name:'Exit (Esc)'})).toBeInTheDocument()
 fireEvent.keyDown(window,{key:'f'});expect(screen.queryByRole('button',{name:'Exit (Esc)'})).not.toBeInTheDocument()
})
test('shortcut ignores keys from children of editable content',async()=>{
 render(<Harness/>);await screen.findByRole('link',{name:'Synthetic camera'})
 const child=document.createElement('span');screen.getByTestId('editor').appendChild(child);fireEvent.keyDown(child,{key:'f'})
 expect(screen.queryByRole('button',{name:'Exit (Esc)'})).not.toBeInTheDocument()
})
