import { beforeEach, afterEach, expect, test, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import ProtectedRoute from '../auth/ProtectedRoute'
import CameraPipProvider from './CameraPipProvider'
import { OrgProvider } from '../../contexts/OrgContext'
import Cameras from '../../pages/printers/Cameras'
import { clearSensitiveBrowserState, getCurrentUser } from '../../permissions'
vi.mock('../../hooks/useWebRTC',()=>({default:()=>({videoRef:{current:null},status:'connecting',retry:vi.fn()})}))
let client:QueryClient
beforeEach(async()=>{
 await clearSensitiveBrowserState();client=new QueryClient({defaultOptions:{queries:{retry:false}}})
 vi.stubGlobal('fetch',vi.fn(async url=>({ok:true,status:200,json:async()=>{
  const path=String(url)
  if(path==='/api/auth/me')return {id:3,username:'synthetic',group_id:1,role:'admin'}
  if(path==='/api/permissions')return {page_access:{},action_access:{}}
  if(path==='/api/cameras')return [{id:9,name:'Synthetic camera',camera_enabled:true,has_camera:true,display_order:0}]
  if(path.includes('/vision'))return {enabled:false}
  return []
 }})))
})
afterEach(()=>{client.clear();vi.unstubAllGlobals()})
test('real permission bootstrap supplies verified identity to camera consumers',async()=>{
 render(<QueryClientProvider client={client}><MemoryRouter><OrgProvider><Routes><Route path="/" element={<ProtectedRoute><CameraPipProvider><Cameras/></CameraPipProvider></ProtectedRoute>}/></Routes></OrgProvider></MemoryRouter></QueryClientProvider>)
 await screen.findByRole('link',{name:'Synthetic camera'})
 expect(getCurrentUser()).toMatchObject({id:3,group_id:1,role:'admin'})
 expect(client.getQueryData(['auth','me'])).toMatchObject({id:3,group_id:1})
 expect(client.getQueryData(['cameras',3,1,'admin',1])).toBeDefined()
 fireEvent.click(screen.getByTitle('Picture-in-Picture'));await screen.findByRole('region',{name:'Picture-in-picture camera'})
 expect(window.localStorage.getItem('odin_user')).toBeNull();expect(window.sessionStorage.getItem('odin_user')).toBeNull()
 await waitFor(()=>expect(fetch).toHaveBeenCalledWith('/api/auth/me',expect.objectContaining({credentials:'include'})))
})
