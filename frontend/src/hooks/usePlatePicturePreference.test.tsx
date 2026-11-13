import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const user = vi.hoisted(() => ({ current: { id: 7 } as { id?: number } | null }))
vi.mock('../permissions', () => ({ getCurrentUser: () => user.current }))

import { usePlatePicturePreference } from './usePlatePicturePreference'

beforeEach(() => { user.current = { id: 7 }; window.localStorage.clear() })
afterEach(() => { cleanup(); window.localStorage.clear() })

it('is off by default', () => {
  const { result } = renderHook(() => usePlatePicturePreference())
  expect(result.current[0]).toBe(false)
})

it('persists per user and survives a remount', () => {
  const first = renderHook(() => usePlatePicturePreference())
  act(() => first.result.current[1](true))
  expect(first.result.current[0]).toBe(true)
  expect(window.localStorage.getItem('odin.printerCards.platePicture.7')).toBe('1')
  first.unmount()
  expect(renderHook(() => usePlatePicturePreference()).result.current[0]).toBe(true)
})

it('turning it off removes the stored choice', () => {
  const { result } = renderHook(() => usePlatePicturePreference())
  act(() => result.current[1](true))
  act(() => result.current[1](false))
  expect(result.current[0]).toBe(false)
  expect(window.localStorage.getItem('odin.printerCards.platePicture.7')).toBeNull()
})

it('another user on the same browser starts with it off', () => {
  const first = renderHook(() => usePlatePicturePreference())
  act(() => first.result.current[1](true))
  first.unmount()
  user.current = { id: 8 }
  expect(renderHook(() => usePlatePicturePreference()).result.current[0]).toBe(false)
})

it('without a signed-in user nothing is stored', () => {
  user.current = null
  const { result } = renderHook(() => usePlatePicturePreference())
  act(() => result.current[1](true))
  expect(window.localStorage.length).toBe(0)
})
