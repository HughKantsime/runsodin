import React from 'react'
import { afterEach, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import SpoolRing from './SpoolRing'

afterEach(cleanup)

it.each([0, 14, 15, 50, 100])('renders the supplied fill level %s with the low warning only below 15', level => {
  const { container } = render(<SpoolRing color="#123abc" material="PETG" level={level} />)
  const circles = container.querySelectorAll('circle')
  expect(circles.length).toBe(level < 15 ? 3 : 2)
  const fill = circles[1]
  expect(fill.getAttribute('stroke')).toBe('#123abc')
  const circumference = Number(fill.getAttribute('stroke-dasharray'))
  const offset = Number(fill.getAttribute('stroke-dashoffset'))
  expect((circumference - offset) / circumference).toBeCloseTo(level / 100)
  expect(screen.getByText('PETG').className.includes('status-warning')).toBe(level < 15)
  if (level < 15) expect(circles[2].getAttribute('stroke')).toBe('var(--status-warning)')
})

it.each([[-20, 0], [150, 100]])('clamps out-of-range level %s to %s', (level, expected) => {
  const { container } = render(<SpoolRing level={level} />)
  const fill = container.querySelectorAll('circle')[1]
  const circumference = Number(fill.getAttribute('stroke-dasharray'))
  expect((circumference - Number(fill.getAttribute('stroke-dashoffset'))) / circumference).toBeCloseTo(expected / 100)
})

it('renders an empty spool as a dashed ring without material or a low-level warning', () => {
  const { container } = render(<SpoolRing empty color="#123abc" material="PETG" level={0} />)
  expect(screen.getByText('Empty')).toBeTruthy()
  expect(screen.queryByText('PETG')).toBeNull()
  const circles = container.querySelectorAll('circle')
  expect(circles.length).toBe(1)
  expect(circles[0].getAttribute('stroke-dasharray')).toBe('3 3')
  expect(circles[0].getAttribute('stroke')).toBe('var(--brand-text-muted)')
})

it('uses a full default fill and applies the requested display size', () => {
  const { container } = render(<SpoolRing size={32} className="synthetic-display" />)
  const svg = container.querySelector('svg')!
  expect(svg.getAttribute('width')).toBe('32')
  expect(svg.getAttribute('height')).toBe('32')
  expect(container.firstElementChild!.classList.contains('synthetic-display')).toBe(true)
  const fill = container.querySelectorAll('circle')[1]
  expect(Number(fill.getAttribute('stroke-dashoffset'))).toBe(0)
  expect(fill.getAttribute('stroke')).toBe('#888')
})
