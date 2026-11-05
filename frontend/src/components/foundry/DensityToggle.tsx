import { useEffect, useState } from 'react'
import { readPreference, writePreference } from '../../hooks/themePreferences'
export type Density = 'comfortable' | 'compact'
const KEY = 'odin-foundry-density'
const EVENT = 'odin-foundry-density-change'
const read = (): Density => readPreference(KEY) === 'compact' ? 'compact' : 'comfortable'
export function useDensity() {
  const [density, setDensity] = useState<Density>(read)
  useEffect(() => {
    const storage = (event: StorageEvent) => { if (!event.key || event.key === KEY) setDensity(read()) }
    const local = (event: Event) => setDensity((event as CustomEvent<Density>).detail)
    window.addEventListener('storage', storage)
    window.addEventListener(EVENT, local)
    return () => { window.removeEventListener('storage', storage); window.removeEventListener(EVENT, local) }
  }, [])
  return [density, (value: Density) => {
    setDensity(value)
    writePreference(KEY, value)
    window.dispatchEvent(new CustomEvent(EVENT, { detail: value }))
  }] as const
}
export default function DensityToggle() {
  const [density, setDensity] = useDensity()
  return <div className="f-density" role="group" aria-label="Density">{(['comfortable', 'compact'] as const).map(value => <button type="button" key={value} aria-pressed={density === value} onClick={() => setDensity(value)}>{value === 'comfortable' ? 'Comfortable' : 'Compact'}</button>)}</div>
}
