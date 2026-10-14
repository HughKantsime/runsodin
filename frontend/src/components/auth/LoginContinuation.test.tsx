import React, { StrictMode } from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import LoginContinuation, { readLoginAuthority } from './LoginContinuation'
const request=vi.fn(), complete=vi.fn(), restart=vi.fn()
const response=(body: unknown,status=200)=>({ok:status===200,status,redirected:false,json:async()=>body})
function start(kind: 'challenge'|'enrollment'='enrollment') {
 vi.stubGlobal('fetch',request)
 render(<StrictMode><LoginContinuation authority={{kind,token:'synthetic-authority'}} onComplete={complete} onRestart={restart}/></StrictMode>)
}
const full=()=>response({token_type:'bearer',access_token:'synthetic.'+btoa(JSON.stringify({sub:'teacher',role:'viewer',exp:Date.now()/1000+60}))+'.signature'})
const setup=()=>response({secret:'A'.repeat(32),qr_code:'data:image/png;base64,synthetic',expires_at:Date.now()/1000+60})
afterEach(()=>{vi.useRealTimers();cleanup();vi.unstubAllGlobals();request.mockReset();complete.mockReset();restart.mockReset()})
it('sets up once and verifies session before completion',async()=>{
 request.mockResolvedValueOnce(setup()).mockResolvedValueOnce(full()).mockResolvedValueOnce(response({username:'teacher',role:'viewer'}))
 start();await screen.findByRole('img');expect(request).toHaveBeenCalledTimes(1);expect(complete).not.toHaveBeenCalled()
 fireEvent.change(screen.getByLabelText('Authentication code'),{target:{value:'123456'}});fireEvent.click(screen.getByText('Verify and sign in'))
 await waitFor(()=>expect(complete).toHaveBeenCalledTimes(1))
 expect(request).toHaveBeenNthCalledWith(2,'/api/auth/mfa/enrollment/confirm',expect.objectContaining({credentials:'include',headers:expect.objectContaining({Authorization:'Bearer synthetic-authority'})}))
 expect(request).toHaveBeenNthCalledWith(3,'/api/auth/me',expect.anything())
})
it.each([401,409,503])('requires restart after uncertainty %s',async status=>{
 request.mockResolvedValueOnce(setup()).mockResolvedValueOnce(response({},status));start();await screen.findByRole('img')
 fireEvent.change(screen.getByLabelText('Authentication code'),{target:{value:'123456'}});fireEvent.click(screen.getByText('Verify and sign in'))
 expect(await screen.findByRole('alert')).toHaveTextContent('Restart sign-in');expect(screen.queryByRole('img')).not.toBeInTheDocument();expect(complete).not.toHaveBeenCalled();expect(request).toHaveBeenCalledTimes(2)
})
it('accepts challenge flow without setup',async()=>{
 request.mockResolvedValueOnce(full()).mockResolvedValueOnce(response({username:'teacher',role:'viewer'}));start('challenge')
 fireEvent.change(screen.getByLabelText('Authentication code'),{target:{value:'123456'}});fireEvent.click(screen.getByText('Verify and sign in'))
 await waitFor(()=>expect(complete).toHaveBeenCalledTimes(1))
 expect(request).toHaveBeenNthCalledWith(1,'/api/auth/mfa/verify',expect.objectContaining({body:JSON.stringify({mfa_token:'synthetic-authority',code:'123456'})}))
})
it('rejects ambiguous purposes and missing credentials',()=>{
 expect(()=>readLoginAuthority({mfa_required:true,mfa_setup_required:true,access_token:'synthetic'})).toThrow()
 expect(()=>readLoginAuthority({mfa_setup_required:true})).toThrow()
})

it('rejects malformed present MFA flags',()=>{expect(()=>readLoginAuthority({mfa_required:'true'})).toThrow()})
it('rejects another account cookie after confirmation',async()=>{
 request.mockResolvedValueOnce(full()).mockResolvedValueOnce(response({username:'other',role:'viewer'}));start('challenge')
 fireEvent.change(screen.getByLabelText('Authentication code'),{target:{value:'123456'}});fireEvent.click(screen.getByText('Verify and sign in'))
 expect(await screen.findByRole('alert')).toHaveTextContent('Restart sign-in');expect(complete).not.toHaveBeenCalled()
})
it('returns immediately when cancellation stalls',async()=>{
 request.mockResolvedValueOnce(setup()).mockReturnValueOnce(new Promise(()=>{}));start();await screen.findByRole('img')
 fireEvent.click(screen.getByText('Cancel and return to sign in'));expect(restart).toHaveBeenCalledTimes(1);expect(complete).not.toHaveBeenCalled()
})

it('erases setup UI when authority expires',async()=>{
 vi.useFakeTimers();request.mockResolvedValueOnce(setup());start()
 await act(async()=>{await Promise.resolve();await Promise.resolve()})
 expect(screen.getByRole('img')).toBeInTheDocument()
 await act(async()=>{vi.advanceTimersByTime(61000)})
 expect(screen.queryByRole('img')).not.toBeInTheDocument();expect(screen.queryByLabelText('Authentication code')).not.toBeInTheDocument()
 expect(screen.getByRole('alert')).toHaveTextContent('expired')
})
it('does not double submit or advance after unmount during confirmation',async()=>{
 let finish!: (value:unknown)=>void
 request.mockReturnValueOnce(new Promise(resolve=>{finish=resolve}));start('challenge')
 fireEvent.change(screen.getByLabelText('Authentication code'),{target:{value:'123456'}})
 const form=screen.getByLabelText('Authentication code').closest('form')!
 fireEvent.submit(form);fireEvent.submit(form);expect(request).toHaveBeenCalledTimes(1)
 cleanup();await act(async()=>finish(full()))
 expect(request).toHaveBeenCalledTimes(1);expect(complete).not.toHaveBeenCalled()
})
it('allows a corrected enrollment code after a definite invalid-code response',async()=>{
 request.mockResolvedValueOnce(setup()).mockResolvedValueOnce(response({},400));start();await screen.findByRole('img')
 fireEvent.change(screen.getByLabelText('Authentication code'),{target:{value:'123456'}});fireEvent.click(screen.getByText('Verify and sign in'))
 expect(await screen.findByRole('alert')).toHaveTextContent('not accepted');expect(screen.getByRole('img')).toBeInTheDocument()
 expect(screen.getByLabelText('Authentication code')).toHaveValue('');expect(request).toHaveBeenCalledTimes(2)
})
it('refuses malformed setup without exposing backend detail',async()=>{
 request.mockResolvedValueOnce(response({secret:'sensitive backend detail'}));start()
 expect(await screen.findByRole('alert')).toHaveTextContent('Restart sign-in');expect(screen.queryByRole('img')).not.toBeInTheDocument()
 expect(screen.getByRole('alert')).not.toHaveTextContent('sensitive')
})
