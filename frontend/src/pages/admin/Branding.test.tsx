import {act,cleanup,render,screen} from '@testing-library/react'
import {afterEach,expect,test,vi} from 'vitest'
import Branding from './Branding'
import { contrastRatio } from '../../utils/contrast'

afterEach(()=>{cleanup();delete document.documentElement.dataset.themeFamily;vi.unstubAllGlobals()})

test('Fjord names every icon-only branding tab on phones', async () => {
 document.documentElement.dataset.themeFamily = 'fjord'
 const css = document.createElement('style')
 css.textContent = '.hidden { display: none }'
 document.head.append(css)
 try {
  vi.stubGlobal('fetch', vi.fn(async () => ({ok: true, json: async () => ({})})))
  render(<Branding />)
  await screen.findByText('App Identity')
  for (const name of ['Identity', 'Sidebar', 'Content', 'Typography']) {
   expect(screen.getByRole('button', { name })).toBeVisible()
  }
 } finally {
  css.remove()
 }
})

test.each(['#d97706', '#123456'])('Fjord preview text contrasts with its draft fill %s', async primary => {
 document.documentElement.dataset.themeFamily = 'fjord'
 vi.stubGlobal('fetch', vi.fn(async () => ({ok: true, json: async () => ({primary_color: primary})})))
 render(<Branding />)
 for (const label of ['Start Job', 'Sign In']) {
  const sample = await screen.findByText(label)
  expect(sample).toHaveStyle({backgroundColor: primary})
  expect(sample).toHaveClass('text-[var(--brand-on-primary)]')
  expect(sample).not.toHaveClass('text-white')
  expect(contrastRatio(sample.style.getPropertyValue('--brand-on-primary'), primary)).toBeGreaterThanOrEqual(4.5)
 }
})

test('switching back to Classic restores the original branding markup', async () => {
 vi.stubGlobal('fetch', vi.fn(async () => ({ok: true, json: async () => ({})})))
 const { container } = render(<Branding />)
 await screen.findByText('App Identity')
 const classic = container.innerHTML
 const identity = screen.getByRole('button', { name: 'Identity' })
 expect(identity).not.toHaveAttribute('aria-label')
 expect(screen.getByText('Start Job')).toHaveClass('text-white')
 await act(async () => { document.documentElement.dataset.themeFamily = 'fjord' })
 expect(identity).toHaveAttribute('aria-label', 'Identity')
 await act(async () => { document.documentElement.dataset.themeFamily = 'classic' })
 expect(container.innerHTML).toBe(classic)
})

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

test('warns about saved custom contrast pairs and preserves the custom values after save', async () => {
 const {fireEvent} = await import('@testing-library/react')
 const {DEFAULT_SURFACE_COLORS} = await import('../../utils/brandingPalette')
 let saved = {...DEFAULT_SURFACE_COLORS, app_name:'Custom farm', text_muted:'#58554a'}
 vi.stubGlobal('fetch', vi.fn(async (_url:any, options:any) => {
  if (options?.method === 'PUT') saved = {...saved, ...JSON.parse(options.body)}
  return {ok:true,json:async()=>saved}
 }))
 render(<Branding />)
 expect(await screen.findByText(/These saved colours/)).toBeInTheDocument()
 expect(screen.getByText(/text muted on card bg:/)).toBeInTheDocument()
 fireEvent.change(screen.getByPlaceholderText('O.D.I.N.'), {target:{value:'Renamed farm'}})
 expect(screen.getByText(/These draft colours/)).toBeInTheDocument()
 fireEvent.click(screen.getByRole('button',{name:'Save Changes'}))
 await screen.findByRole('button',{name:'Saved!'})
 expect(saved.text_muted).toBe('#58554a')
 expect(screen.getByText(/These saved colours/)).toBeInTheDocument()
})
