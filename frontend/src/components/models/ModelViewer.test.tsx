import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { fetchAPI } from '../../api'
import ModelViewer from './ModelViewer'

const draw = vi.hoisted(() => vi.fn())
vi.mock('three', async (importOriginal) => ({
  ...await importOriginal<typeof import('three')>(),
  WebGLRenderer: class {
    domElement = document.createElement('canvas')
    shadowMap = {}
    setSize() {}
    setPixelRatio() {}
    dispose() {}
    forceContextLoss() {}
    render = draw
  },
}))
vi.mock('../../api', () => ({ fetchAPI: vi.fn() }))
afterEach(() => { cleanup(); vi.restoreAllMocks() })

test('an absent mesh shows an empty preview without suggesting another upload', async () => {
  vi.mocked(fetchAPI).mockRejectedValueOnce(Object.assign(new Error('Preview not found'), { status: 404 }))
  const report = vi.spyOn(console, 'error').mockImplementation(() => {})
  render(<ModelViewer fileId={12} onClose={() => {}} />)
  expect(await screen.findByText('No 3D preview available')).toBeInTheDocument()
  expect(screen.queryByText(/re-upload/i)).not.toBeInTheDocument()
  expect(report).not.toHaveBeenCalled()
})

test('a failed mesh request still displays the server error', async () => {
  vi.mocked(fetchAPI).mockRejectedValueOnce(Object.assign(new Error('Storage unavailable'), { status: 503 }))
  vi.spyOn(console, 'error').mockImplementation(() => {})
  render(<ModelViewer fileId={12} onClose={() => {}} />)
  expect(await screen.findByText('Storage unavailable')).toBeInTheDocument()
})

test('a rendered triangle has no empty-state overlay', async () => {
  vi.mocked(fetchAPI).mockResolvedValueOnce({
    vertices: [0, 0, 0, 1, 0, 0, 0, 1, 0], triangles: [0, 1, 2],
    vertex_count: 3, triangle_count: 1,
  })
  render(<ModelViewer fileId={12} onClose={() => {}} />)
  await waitFor(() => expect(draw).toHaveBeenCalled())
  expect(await screen.findByText(/3 vertices · 1 triangles/)).toBeInTheDocument()
  expect(screen.queryByText('No 3D preview available')).not.toBeInTheDocument()
})
