import {cleanup,render,screen,waitFor} from '@testing-library/react'
import {afterEach,expect,test,vi} from 'vitest'
import {BrandingProvider,useBranding,useBrandingUpdate} from './BrandingContext'

function Probe(){const brand=useBranding();return <div><span data-testid="name">{brand.app_name}</span><span data-testid="logo">{brand.logo_url || 'No logo'}</span></div>}
afterEach(()=>{cleanup();vi.unstubAllGlobals();document.title='';document.head.querySelectorAll('link[rel="icon"]').forEach(e=>e.remove())})
const globalBrand={app_name:'Synthetic global',logo_url:'/static/branding/logo-global.png',app_subtitle:'Synthetic subtitle',favicon_url:null}
const response=(data:any,ok=true)=>({ok,json:async()=>data})

test('organization identity overlays global branding only for the current authenticated group',async()=>{
 const fetcher=vi.fn(async(url:any)=>url==='/api/branding'?response(globalBrand):url==='/api/auth/me'?response({group_id:7}):url==='/api/license'?response({tier:'pro',features:['branding']}):url==='/api/orgs/7/settings'?response({branding_app_name:'Synthetic own org',branding_logo_url:'/static/branding/logo-own.png'}):Promise.reject(new Error('Unexpected cross-organization request')))
 vi.stubGlobal('fetch',fetcher)
 render(<BrandingProvider><Probe /></BrandingProvider>)
 await waitFor(()=>expect(screen.getByTestId('name')).toHaveTextContent('Synthetic own org'))
 expect(screen.getByTestId('logo')).toHaveTextContent('/static/branding/logo-own.png')
 expect(fetcher.mock.calls.map(call=>call[0])).toEqual(['/api/branding','/api/auth/me','/api/license','/api/orgs/7/settings'])
 expect(document.title).toBe('Synthetic own org Synthetic subtitle')
})

test.each([null,undefined])('global identity without a group never fetches organization settings (%s)',async group=>{
 const fetcher=vi.fn(async(url:any)=>url==='/api/branding'?response(globalBrand):response({group_id:group}))
 vi.stubGlobal('fetch',fetcher)
 render(<BrandingProvider><Probe /></BrandingProvider>)
 await waitFor(()=>expect(screen.getByTestId('name')).toHaveTextContent('Synthetic global'))
 expect(fetcher).toHaveBeenCalledTimes(2)
 expect(screen.getByTestId('logo')).toHaveTextContent('/static/branding/logo-global.png')
})

test('denied organization settings preserve global identity without reading denied JSON',async()=>{
 const deniedBody=vi.fn(async()=>({branding_app_name:'Must not display'}))
 vi.stubGlobal('fetch',vi.fn(async(url:any)=>url==='/api/branding'?response(globalBrand):url==='/api/auth/me'?response({group_id:7}):{ok:false,json:deniedBody}))
 render(<BrandingProvider><Probe /></BrandingProvider>)
 await waitFor(()=>expect(screen.getByTestId('name')).toHaveTextContent('Synthetic global'))
 expect(deniedBody).not.toHaveBeenCalled()
 expect(document.title).toBe('Synthetic global Synthetic subtitle')
})

test('failed global request retains usable defaults without organization requests',async()=>{
 const fetcher=vi.fn(async()=>{throw new Error('Synthetic unavailable')})
 vi.stubGlobal('fetch',fetcher)
 render(<BrandingProvider><Probe /></BrandingProvider>)
 expect(screen.getByTestId('name')).toHaveTextContent('O.D.I.N.')
 await waitFor(()=>expect(fetcher).toHaveBeenCalledTimes(1))
 expect(screen.getByTestId('logo')).toHaveTextContent('No logo')
})

test('failed branding load restores default document identity rather than stale metadata',async()=>{
 document.title='Synthetic stale organization'
 const icon=document.createElement('link');icon.rel='icon';icon.href='/static/branding/favicon-stale.png';document.head.appendChild(icon)
 vi.stubGlobal('fetch',vi.fn(async()=>({ok:false,json:async()=>({})})))
 render(<BrandingProvider><Probe /></BrandingProvider>)
 await waitFor(()=>expect(document.title).toBe('O.D.I.N. Print Farm Management'))
 expect(document.querySelector('link[rel="icon"]')).toHaveAttribute('href','/favicon.svg')
})

test('theme transitions retain loaded custom colors and release light-mode surfaces',async()=>{
 const colors={...globalBrand,primary_color:'#445566',accent_color:'#667788',card_bg:'#223344',sidebar_bg:'#334455'}
 vi.stubGlobal('fetch',vi.fn(async(url:any)=>url==='/api/branding'?response(colors):response({group_id:null})))
 document.documentElement.classList.remove('light')
 render(<BrandingProvider><Probe /></BrandingProvider>)
 await waitFor(()=>expect(document.documentElement.style.getPropertyValue('--brand-card-bg')).toBe('#223344'))
 document.documentElement.classList.add('light')
 await waitFor(()=>expect(document.documentElement.style.getPropertyValue('--brand-card-bg')).toBe(''))
 expect(document.documentElement.style.getPropertyValue('--brand-accent')).toBe('#667788')
 document.documentElement.classList.remove('light')
 await waitFor(()=>expect(document.documentElement.style.getPropertyValue('--brand-card-bg')).toBe('#223344'))
 expect(document.documentElement.style.getPropertyValue('--brand-primary')).toBe('#445566')
})

test('a saved theme remains authoritative after toggling light and dark mode',async()=>{
 const {default:Branding}=await import('./pages/admin/Branding')
 const {fireEvent}=await import('@testing-library/react')
 document.documentElement.classList.remove('light')
 let saved={...globalBrand,primary_color:'#445566',accent_color:'#667788'}
 vi.stubGlobal('fetch',vi.fn(async(url:any,options:any)=>{
  if(url==='/api/auth/me')return response({group_id:null})
  if(options?.method==='PUT')saved={...saved,...JSON.parse(options.body),primary_color:'#112233'}
  return response(saved)
 }))
 render(<BrandingProvider><Branding /></BrandingProvider>)
 const input=await screen.findByPlaceholderText('O.D.I.N.')
 fireEvent.change(input,{target:{value:'Synthetic saved brand'}})
 fireEvent.click(screen.getByRole('button',{name:'Save Changes'}))
 await screen.findByRole('button',{name:'Saved!'})
 expect(document.documentElement.style.getPropertyValue('--brand-primary')).toBe('#112233')
 document.documentElement.classList.add('light')
 await waitFor(()=>expect(document.documentElement.style.getPropertyValue('--brand-card-bg')).toBe(''))
 document.documentElement.classList.remove('light')
 await waitFor(()=>expect(document.documentElement.style.getPropertyValue('--brand-primary')).toBe('#112233'))
})

function ConfirmedUpdateProbe({patch}:{patch:any}) {
 const update=useBrandingUpdate()
 return <><Probe /><button onClick={()=>update(patch)}>Apply confirmed response</button></>
}

test('a delayed initial response cannot overwrite a confirmed update',async()=>{
 const {fireEvent,act}=await import('@testing-library/react')
 let resolveInitial:any
 const initial=new Promise(resolve=>{resolveInitial=resolve})
 vi.stubGlobal('fetch',vi.fn(async(url:any)=>url==='/api/branding'?initial:response({group_id:null})))
 document.documentElement.classList.remove('light')
 render(<BrandingProvider><ConfirmedUpdateProbe patch={{app_name:'Synthetic confirmed',primary_color:'#112233'}} /></BrandingProvider>)
 fireEvent.click(screen.getByRole('button',{name:'Apply confirmed response'}))
 expect(screen.getByTestId('name')).toHaveTextContent('Synthetic confirmed')
 await act(async()=>{resolveInitial(response({...globalBrand,primary_color:'#445566'}));await initial})
 expect(screen.getByTestId('name')).toHaveTextContent('Synthetic confirmed')
 document.documentElement.classList.add('light')
 await waitFor(()=>expect(document.documentElement.style.getPropertyValue('--brand-card-bg')).toBe(''))
 document.documentElement.classList.remove('light')
 await waitFor(()=>expect(document.documentElement.style.getPropertyValue('--brand-primary')).toBe('#112233'))
})

test('partial asset updates preserve confirmed theme settings and organization overlay',async()=>{
 const {fireEvent}=await import('@testing-library/react')
 vi.stubGlobal('fetch',vi.fn(async(url:any)=>url==='/api/branding'?response({...globalBrand,primary_color:'#112233'}):url==='/api/auth/me'?response({group_id:7}):url==='/api/license'?response({tier:'pro',features:['branding']}):response({branding_app_name:'Synthetic own org'})))
 render(<BrandingProvider><ConfirmedUpdateProbe patch={{favicon_url:'/static/branding/favicon-confirmed.png'}} /></BrandingProvider>)
 await waitFor(()=>expect(screen.getByTestId('name')).toHaveTextContent('Synthetic own org'))
 fireEvent.click(screen.getByRole('button',{name:'Apply confirmed response'}))
 expect(screen.getByTestId('name')).toHaveTextContent('Synthetic own org')
 expect(screen.getByTestId('logo')).toHaveTextContent('/static/branding/logo-global.png')
 expect(document.documentElement.style.getPropertyValue('--brand-primary')).toBe('#112233')
 expect(document.querySelector('link[rel="icon"]')).toHaveAttribute('href','/static/branding/favicon-confirmed.png')
})

test('an asset patch before initial loading preserves the saved theme once loading finishes',async()=>{
 const {fireEvent,act}=await import('@testing-library/react')
 let resolveInitial:any
 const initial=new Promise(resolve=>{resolveInitial=resolve})
 vi.stubGlobal('fetch',vi.fn(async(url:any)=>url==='/api/branding'?initial:response({group_id:7})))
 // Organization response is independently delayed by the initial global request.
 render(<BrandingProvider><ConfirmedUpdateProbe patch={{favicon_url:'/static/branding/favicon-confirmed.png'}} /></BrandingProvider>)
 fireEvent.click(screen.getByRole('button',{name:'Apply confirmed response'}))
 await act(async()=>{resolveInitial(response({...globalBrand,primary_color:'#112233'}));await initial})
 await waitFor(()=>expect(document.documentElement.style.getPropertyValue('--brand-primary')).toBe('#112233'))
 expect(document.querySelector('link[rel="icon"]')).toHaveAttribute('href','/static/branding/favicon-confirmed.png')
})

test('page-loaded favicon upload survives delayed provider settings and theme toggles',async()=>{
 const {default:Branding}=await import('./pages/admin/Branding')
 const {fireEvent,act}=await import('@testing-library/react')
 let resolveProvider:any;let reads=0
 const delayed=new Promise(resolve=>{resolveProvider=resolve})
 const initial={...globalBrand,primary_color:'#112233'}
 vi.stubGlobal('fetch',vi.fn(async(url:any,options:any)=>{
  if(options?.method==='POST')return response({favicon_url:'/static/branding/favicon-confirmed.png'})
  if(url==='/api/auth/me')return response({group_id:null})
  reads++
  return reads===1?response(initial):delayed
 }))
 document.documentElement.classList.remove('light')
 render(<BrandingProvider><Branding /></BrandingProvider>)
 await screen.findByPlaceholderText('O.D.I.N.')
 fireEvent.change(document.querySelectorAll('input[type="file"]')[1],{target:{files:[new File(['synthetic'],'synthetic.png',{type:'image/png'})]}})
 await screen.findByAltText('Favicon')
 await act(async()=>{resolveProvider(response(initial));await delayed})
 document.documentElement.classList.add('light')
 await waitFor(()=>expect(document.documentElement.style.getPropertyValue('--brand-card-bg')).toBe(''))
 document.documentElement.classList.remove('light')
 await waitFor(()=>expect(document.documentElement.style.getPropertyValue('--brand-primary')).toBe('#112233'))
 expect(document.querySelector('link[rel="icon"]')).toHaveAttribute('href','/static/branding/favicon-confirmed.png')
})

test.each([['community',[]],['expired pro',[]],['pro without branding entitlement',['orders']]])('stored org branding is ignored without the branding entitlement (%s)',async(_label,features)=>{
 const fetcher=vi.fn(async(url:any)=>url==='/api/branding'?response(globalBrand):url==='/api/auth/me'?response({group_id:7}):url==='/api/license'?response({tier:'community',features}):url==='/api/orgs/7/settings'?response({branding_app_name:'Downgraded white label',branding_logo_url:'/static/branding/logo-own.png'}):Promise.reject(new Error('unexpected')))
 vi.stubGlobal('fetch',fetcher)
 render(<BrandingProvider><Probe /></BrandingProvider>)
 await waitFor(()=>expect(fetcher.mock.calls.map(call=>call[0])).toEqual(['/api/branding','/api/auth/me','/api/license']))
 await waitFor(()=>expect(screen.getByTestId('name')).toHaveTextContent('Synthetic global'))
 expect(screen.getByTestId('logo')).toHaveTextContent('/static/branding/logo-global.png')
})
