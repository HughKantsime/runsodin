import { afterEach, test, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import CameraPipPlayer from './CameraPipPlayer'
vi.mock('../../hooks/useWebRTC',()=>({default:()=>({videoRef:{current:null},status:'connecting',retry:vi.fn()})}))
afterEach(()=>{vi.restoreAllMocks();vi.unstubAllGlobals()})
test('mobile player leaves fixed bottom navigation uncovered after open and resize',()=>{
 vi.stubGlobal('innerWidth',390);vi.stubGlobal('innerHeight',844)
 vi.spyOn(Element.prototype,'getBoundingClientRect').mockImplementation(function(){
  return this.matches('nav.fixed')?{top:innerHeight-56,bottom:innerHeight,left:0,right:innerWidth,width:innerWidth,height:56,x:0,y:innerHeight-56,toJSON:()=>({})}:{top:0,bottom:0,left:0,right:0,width:0,height:0,x:0,y:0,toJSON:()=>({})}
 })
 render(<><nav className="fixed">Bottom navigation</nav><CameraPipPlayer camera={{id:9,name:'Synthetic',camera_enabled:true,has_camera:true,display_order:0}} onClose={()=>{}}/></>)
 const player=screen.getByRole('region');expect(parseFloat(player.style.top)+parseFloat(player.style.height)).toBeLessThanOrEqual(788)
 vi.stubGlobal('innerHeight',300);fireEvent(window,new Event('resize'));expect(parseFloat(player.style.top)+parseFloat(player.style.height)).toBeLessThanOrEqual(244)
})
test('hidden desktop bottom navigation does not reduce usable viewport',()=>{
 vi.stubGlobal('innerWidth',1280);vi.stubGlobal('innerHeight',800)
 render(<><nav className="fixed" style={{display:'none'}}>Hidden navigation</nav><CameraPipPlayer camera={{id:9,name:'Synthetic',camera_enabled:true,has_camera:true,display_order:0}} onClose={()=>{}}/></>)
 const player=screen.getAllByRole('region').at(-1)!;expect(parseFloat(player.style.top)+parseFloat(player.style.height)).toBe(780)
})
