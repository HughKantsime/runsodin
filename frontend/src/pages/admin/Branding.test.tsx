import {cleanup,render,screen} from '@testing-library/react'
import {afterEach,expect,test,vi} from 'vitest'
import Branding from './Branding'

afterEach(()=>{cleanup();vi.unstubAllGlobals()})

test('failed initial branding request shows an actionable error instead of loading forever',async()=>{
 vi.stubGlobal('fetch',vi.fn(async()=>{throw new Error('Synthetic unavailable')}))
 render(<Branding />)
 expect(await screen.findByText('Failed to load branding config')).toBeInTheDocument()
 expect(screen.getByRole('button',{name:'Retry'})).toBeInTheDocument()
 expect(screen.queryByText('Loading branding configuration...')).not.toBeInTheDocument()
})

test('initial HTTP failure recovers after an explicit retry',async()=>{
 const fetcher=vi.fn().mockResolvedValueOnce({ok:false,json:async()=>({detail:'Synthetic unavailable'})}).mockResolvedValueOnce({ok:true,json:async()=>({app_name:'Synthetic recovered',favicon_url:null})})
 vi.stubGlobal('fetch',fetcher)
 render(<Branding />)
 await screen.findByText('Failed to load branding config')
 const {fireEvent}=await import('@testing-library/react')
 fireEvent.click(screen.getByRole('button',{name:'Retry'}))
 await screen.findByDisplayValue('Synthetic recovered')
 expect(screen.queryByText('Failed to load branding config')).not.toBeInTheDocument()
 expect(fetcher).toHaveBeenCalledTimes(2)
})

test('successful favicon removal restores the default browser icon',async()=>{
 const {fireEvent,waitFor}=await import('@testing-library/react')
 const {applyBrandingFavicon}=await import('../../BrandingContext')
 applyBrandingFavicon('/static/branding/favicon-synthetic.png')
 vi.stubGlobal('fetch',vi.fn(async(_url:any,options:any)=> options?.method==='DELETE'?{ok:true,json:async()=>({favicon_url:null})}:{ok:true,json:async()=>({app_name:'Synthetic brand',favicon_url:'/static/branding/favicon-synthetic.png'})}))
 render(<Branding />)
 await screen.findByAltText('Favicon')
 fireEvent.click(screen.getByRole('button',{name:'Remove'}))
 await waitFor(()=>expect(document.querySelector('link[rel="icon"]')).toHaveAttribute('href','/favicon.svg'))
 expect(screen.queryByAltText('Favicon')).not.toBeInTheDocument()
})

test('favicon upload selects the returned immutable URL and clears stale MIME hints',async()=>{
 const {fireEvent,waitFor}=await import('@testing-library/react')
 const {applyBrandingFavicon}=await import('../../BrandingContext')
 applyBrandingFavicon(null)
 document.querySelector('link[rel="icon"]')?.setAttribute('type','image/svg+xml')
 const selected='/static/branding/favicon-0123456789abcdef0123456789abcdef.png'
 vi.stubGlobal('fetch',vi.fn(async(_url:any,options:any)=>options?.method==='POST'?{ok:true,json:async()=>({favicon_url:selected})}:{ok:true,json:async()=>({app_name:'Synthetic brand',favicon_url:null})}))
 render(<Branding />)
 await screen.findByDisplayValue('Synthetic brand')
 const inputs=document.querySelectorAll<HTMLInputElement>('input[type="file"]')
 fireEvent.change(inputs[1],{target:{files:[new File(['synthetic fixture'],'synthetic.png',{type:'image/png'})]}})
 await waitFor(()=>expect(document.querySelector('link[rel="icon"]')).toHaveAttribute('href',selected))
 expect(document.querySelector('link[rel="icon"]')).not.toHaveAttribute('type')
 expect(screen.getByAltText('Favicon')).toHaveAttribute('src',selected)
})
