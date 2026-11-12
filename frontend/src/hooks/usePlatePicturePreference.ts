import { useCallback, useState } from 'react'
import { getCurrentUser } from '../permissions'

// Per-user display preference "Show plate picture on printer cards"; off by default. Stored per
// browser under the signed-in user's id, so another user on a shared machine starts with it off.
const KEY_PREFIX = 'odin.printerCards.platePicture.'

function storageKey(): string | null {
  const id = getCurrentUser()?.id
  return typeof id === 'number' ? `${KEY_PREFIX}${id}` : null
}

export function readPlatePicturePreference(): boolean {
  try {
    const key = storageKey()
    return key !== null && window.localStorage.getItem(key) === '1'
  } catch {
    return false
  }
}

export function usePlatePicturePreference(): [boolean, (value: boolean) => void] {
  const [value, setValue] = useState(readPlatePicturePreference)
  const update = useCallback((next: boolean) => {
    setValue(next)
    try {
      const key = storageKey()
      if (key === null) return
      if (next) window.localStorage.setItem(key, '1')
      else window.localStorage.removeItem(key)
    } catch {
      // storage unavailable: the choice lasts for this page view only
    }
  }, [])
  return [value, update]
}
