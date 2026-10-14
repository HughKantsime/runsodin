import React from 'react'
import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import {cleanup,fireEvent,render,screen} from '@testing-library/react'
import {MemoryRouter,useLocation} from 'react-router-dom'
import useKeyboardShortcuts from './useKeyboardShortcuts'
import KeyboardShortcutsModal from '../components/shared/KeyboardShortcutsModal'
import ThemeToggle from '../components/layout/ThemeToggle'
function Harness(){const s=useKeyboardShortcuts();const l=useLocation();return <><output data-testid="path">{l.pathname}</output><input aria-label="Editing"/>{s.showHelp&&<KeyboardShortcutsModal onClose={()=>s.setShowHelp(false)}/>}</>}
function show(path='/orders'){return render(<MemoryRouter initialEntries={[path]}><Harness/></MemoryRouter>)}
function key(value:string,target:HTMLElement|Document=document){fireEvent.keyDown(target,{key:value})}
beforeEach(()=>{localStorage.clear();document.documentElement.classList.remove('light');vi.stubGlobal('matchMedia',()=>({matches:false,addEventListener:vi.fn(),removeEventListener:vi.fn()}))})
afterEach(()=>{cleanup();vi.unstubAllGlobals();vi.restoreAllMocks()})
it('opens actual help and closes with Escape',()=>{show();key('?');expect(screen.getByRole('dialog')).toBeTruthy();expect(screen.getByText('Keyboard Shortcuts')).toBeTruthy();key('Escape');expect(screen.queryByRole('dialog')).toBeNull()})
it.each([['d','/'],['j','/jobs'],['p','/printers']])('navigates g then %s to %s',(letter,path)=>{show();key('g');key(letter);expect(screen.getByTestId('path').textContent).toBe(path)})
it('protects editing from navigation and help shortcuts',()=>{show();const input=screen.getByRole('textbox',{name:'Editing'});key('g',input);key('j',input);key('?',input);expect(screen.getByTestId('path').textContent).toBe('/orders');expect(screen.queryByRole('dialog')).toBeNull()})
it('protects the page behind an open modal',()=>{show();key('?');key('g');key('j');expect(screen.getByTestId('path').textContent).toBe('/orders')})
it('dispatches the context-specific order creation event',()=>{show();const listener=vi.fn();window.addEventListener('new-item',listener);try{key('n');expect(listener).toHaveBeenCalledTimes(1);expect(listener.mock.calls[0][0].detail).toEqual({page:'/orders'})}finally{window.removeEventListener('new-item',listener)}})
it('navigates N on jobs to upload',()=>{show('/jobs');key('n');expect(screen.getByTestId('path').textContent).toBe('/upload')})
it('toggles actual theme button and persists across remount',()=>{let view=render(<ThemeToggle/>);fireEvent.click(screen.getByRole('button',{name:'Switch to light mode'}));expect(document.documentElement.classList.contains('light')).toBe(true);expect(localStorage.getItem('odin-theme')).toBe('light');view.unmount();view=render(<ThemeToggle/>);expect(screen.getByRole('button',{name:'Switch to dark mode'})).toBeTruthy();fireEvent.click(screen.getByRole('button',{name:'Switch to dark mode'}));expect(localStorage.getItem('odin-theme')).toBe('dark');expect(document.documentElement.classList.contains('light')).toBe(false)})
