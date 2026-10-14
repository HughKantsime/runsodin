import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import Login from './Login'
const mocks=vi.hoisted(()=>({navigate:vi.fn(),refresh:vi.fn()}))
vi.mock('react-router-dom',()=>({useNavigate:()=>mocks.navigate}))
vi.mock('../../BrandingContext',()=>({useBranding:()=>({app_name:'ODIN',app_subtitle:'Print management'})}))
vi.mock('../../permissions',()=>({refreshPermissions:mocks.refresh}))
vi.mock('../../api',()=>({auth:{capabilities:async()=>({smtp_enabled:false})}}))
vi.mock('../../components/auth/SSOButton',()=>({default:()=>null}))
const request=vi.fn()
const response=(body:unknown,status=200)=>({ok:status===200,status,redirected:false,json:async()=>body})
const full=()=>response({token_type:'bearer',access_token:'synthetic.'+btoa(JSON.stringify({sub:'teacher',role:'viewer',exp:Date.now()/1000+60}))+'.signature'})
beforeEach(()=>{vi.stubGlobal('fetch',request);vi.stubGlobal('__APP_VERSION__','test')})
afterEach(()=>{cleanup();vi.unstubAllGlobals();request.mockReset();mocks.navigate.mockReset();mocks.refresh.mockReset()})
function login(){render(<Login/>);fireEvent.change(screen.getByLabelText('Username'),{target:{value:'teacher'}});fireEvent.change(screen.getByLabelText('Password'),{target:{value:'synthetic-password'}});fireEvent.submit(screen.getByLabelText('Username').closest('form')!)}
it.each(['mfa_required','mfa_setup_required'])('holds password sign-in for %s until confirmed session',async flag=>{
 request.mockResolvedValueOnce(response({[flag]:true,access_token:'synthetic-authority'}))
 if(flag==='mfa_setup_required')request.mockResolvedValueOnce(response({secret:'A'.repeat(32),qr_code:'data:image/png;base64,synthetic',expires_at:Date.now()/1000+60}))
 request.mockResolvedValueOnce(full()).mockResolvedValueOnce(response({username:'teacher',role:'viewer'}))
 login();await screen.findByRole('region',{name:'Complete sign-in'})
 if(flag==='mfa_setup_required')await screen.findByRole('img')
 expect(mocks.navigate).not.toHaveBeenCalled();expect(mocks.refresh).not.toHaveBeenCalled()
 expect(request.mock.calls.some(([url])=>url==='/api/auth/me')).toBe(false)
 fireEvent.change(screen.getByLabelText('Authentication code'),{target:{value:'123456'}});fireEvent.click(screen.getByText('Verify and sign in'))
 await waitFor(()=>expect(mocks.navigate).toHaveBeenCalledWith('/'))
 expect(mocks.refresh).toHaveBeenCalledTimes(1)
 expect(localStorage.getItem('access_token')).toBeNull();expect(sessionStorage.getItem('mfa_token')).toBeNull()
})
it('refuses navigation when a different account cookie remains',async()=>{
 request.mockResolvedValueOnce(full()).mockResolvedValueOnce(response({username:'other',role:'viewer'}));login()
 await screen.findByText('Invalid session');expect(mocks.navigate).not.toHaveBeenCalled();expect(mocks.refresh).not.toHaveBeenCalled()
})
