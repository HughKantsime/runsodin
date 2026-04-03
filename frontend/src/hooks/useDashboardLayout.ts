/**
 * useDashboardLayout — persistent dashboard customization.
 *
 * Users can reorder sections, show/hide widgets, and choose grid density.
 * Layout persists in localStorage.
 */

import { useState, useCallback } from 'react'

export interface DashboardSection {
  id: string
  label: string
  visible: boolean
  order: number
}

const DEFAULT_SECTIONS: DashboardSection[] = [
  { id: 'stats', label: 'Quick Stats', visible: true, order: 0 },
  { id: 'printers', label: 'Printer Grid', visible: true, order: 1 },
  { id: 'jobs', label: 'Active Jobs', visible: true, order: 2 },
  { id: 'alerts', label: 'Alerts', visible: true, order: 3 },
  { id: 'cameras', label: 'Camera Preview', visible: true, order: 4 },
  { id: 'maintenance', label: 'Maintenance', visible: true, order: 5 },
  { id: 'recent', label: 'Recent Prints', visible: true, order: 6 },
  { id: 'utilization', label: 'Utilization', visible: false, order: 7 },
]

const STORAGE_KEY = 'odin_dashboard_layout'
const DENSITY_KEY = 'odin_dashboard_density'

export type DashboardDensity = 'compact' | 'normal' | 'spacious'

function loadLayout(): DashboardSection[] {
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved) {
      const parsed = JSON.parse(saved) as DashboardSection[]
      // Merge with defaults to pick up new sections added in updates
      const savedIds = new Set(parsed.map(s => s.id))
      const merged = [
        ...parsed,
        ...DEFAULT_SECTIONS.filter(s => !savedIds.has(s.id)),
      ]
      return merged.sort((a, b) => a.order - b.order)
    }
  } catch {}
  return DEFAULT_SECTIONS
}

function loadDensity(): DashboardDensity {
  try {
    const saved = localStorage.getItem(DENSITY_KEY)
    if (saved && ['compact', 'normal', 'spacious'].includes(saved)) {
      return saved as DashboardDensity
    }
  } catch {}
  return 'normal'
}

export function useDashboardLayout() {
  const [sections, setSections] = useState<DashboardSection[]>(loadLayout)
  const [density, setDensityState] = useState<DashboardDensity>(loadDensity)

  const saveLayout = useCallback((updated: DashboardSection[]) => {
    setSections(updated)
    localStorage.setItem(STORAGE_KEY, JSON.stringify(updated))
  }, [])

  const toggleSection = useCallback((id: string) => {
    const updated = sections.map(s =>
      s.id === id ? { ...s, visible: !s.visible } : s
    )
    saveLayout(updated)
  }, [sections, saveLayout])

  const moveSection = useCallback((id: string, direction: 'up' | 'down') => {
    const idx = sections.findIndex(s => s.id === id)
    if (idx === -1) return
    if (direction === 'up' && idx === 0) return
    if (direction === 'down' && idx === sections.length - 1) return

    const updated = [...sections]
    const swapIdx = direction === 'up' ? idx - 1 : idx + 1
    const temp = updated[idx].order
    updated[idx] = { ...updated[idx], order: updated[swapIdx].order }
    updated[swapIdx] = { ...updated[swapIdx], order: temp }
    saveLayout(updated.sort((a, b) => a.order - b.order))
  }, [sections, saveLayout])

  const resetLayout = useCallback(() => {
    saveLayout(DEFAULT_SECTIONS)
    setDensityState('normal')
    localStorage.removeItem(DENSITY_KEY)
  }, [saveLayout])

  const setDensity = useCallback((d: DashboardDensity) => {
    setDensityState(d)
    localStorage.setItem(DENSITY_KEY, d)
  }, [])

  const visibleSections = sections.filter(s => s.visible).sort((a, b) => a.order - b.order)

  return {
    sections,
    visibleSections,
    density,
    toggleSection,
    moveSection,
    resetLayout,
    setDensity,
  }
}
