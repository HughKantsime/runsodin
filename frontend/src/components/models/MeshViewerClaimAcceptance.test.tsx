import React from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import ModelViewer from './ModelViewer'
import Models from '../../pages/models/Models'
const boundary = vi.hoisted(() => ({ instances: [] as any[], mesh: vi.fn(), models: vi.fn() }))
// Only GPU/WebGL rendering is replaced. Cameras, scene, vectors, geometry, and orbit code are real.
vi.mock('three', async importOriginal => {
  const actual = await importOriginal<typeof import('three')>()
  class WebGLRenderer {
    domElement = document.createElement('canvas')
    shadowMap = { enabled: false }
    scene: any; camera: any
    setSize = vi.fn(); setPixelRatio = vi.fn(); dispose = vi.fn(); forceContextLoss = vi.fn()
    constructor() { boundary.instances.push(this) }
    render(scene: any, camera: any) { this.scene = scene; this.camera = camera }
  }
  return { ...actual, WebGLRenderer }
})
vi.mock('../../api', () => ({ fetchAPI: boundary.mesh, models: { listWithPricing: boundary.models }, printers: {}, filaments: {} }))
vi.mock('../../contexts/OrgContext', () => ({ useOrg: () => ({ orgId: 17 }) }))
vi.mock('../../permissions', () => ({ canDo: () => true }))
const fixture = { vertices: [10,20,30,30,20,30,10,40,50], triangles: [0,1,2], vertex_count: 3, triangle_count: 1 }
const clients: QueryClient[] = []
async function viewer() {
  const view = render(<ModelViewer modelId={9} modelName="Fixture geometry" onClose={vi.fn()} />)
  await screen.findByText('3 vertices · 1 triangles'); await waitFor(() => expect(boundary.instances).toHaveLength(1))
  const renderer = boundary.instances[0]; return { ...view, renderer, camera: renderer.camera, canvas: renderer.domElement as HTMLCanvasElement }
}
function mouse(canvas: HTMLElement, button: number, dx: number, dy: number) { fireEvent.mouseDown(canvas, { button, clientX: 100, clientY: 100 }); fireEvent.mouseMove(canvas, { clientX: 100+dx, clientY: 100+dy }); fireEvent.mouseUp(canvas, { button }) }
beforeEach(() => {
  boundary.instances.length = 0; vi.clearAllMocks(); boundary.mesh.mockResolvedValue(fixture)
  boundary.models.mockResolvedValue([{ id: 9, name: 'Fixture library model', print_file_id: 51, source_file: 'fixture.3mf' }])
  vi.stubGlobal('requestAnimationFrame', vi.fn(() => 1)); vi.stubGlobal('cancelAnimationFrame', vi.fn())
  vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(800); vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(600)
})
afterEach(() => { cleanup(); clients.forEach(c => c.clear()); clients.length = 0; vi.restoreAllMocks(); vi.unstubAllGlobals() })
it('mesh viewer builds real centered/scaled geometry and camera from synthetic mesh boundary', async () => {
  const { renderer, camera } = await viewer(); expect(boundary.mesh).toHaveBeenCalledWith('/models/9/mesh')
  const mesh = renderer.scene.children.find((child: any) => child.isMesh); mesh.geometry.computeBoundingBox()
  expect(mesh.geometry.getAttribute('position').count).toBe(3); expect(mesh.geometry.index.count).toBe(3)
  expect(mesh.geometry.boundingBox.min.toArray()).toEqual([-50,-50,-50]); expect(mesh.geometry.boundingBox.max.toArray()).toEqual([50,50,50]); expect(camera.aspect).toBeCloseTo(4/3)
})
it('F02-2.8-31 left drag rotates real camera while retaining orbit distance, mouseup ends motion', async () => {
  const { camera, canvas } = await viewer(); const start = camera.position.clone(); const radius = start.length()
  fireEvent.mouseMove(canvas, { clientX: 110, clientY: 120 }); expect(camera.position.toArray()).toEqual(start.toArray())
  mouse(canvas,0,20,10); expect(camera.position.equals(start)).toBe(false); expect(camera.position.length()).toBeCloseTo(radius)
  const stopped = camera.position.clone(); fireEvent.mouseMove(canvas, { clientX: 150, clientY: 130 }); expect(camera.position.toArray()).toEqual(stopped.toArray())
})
it('F02-2.8-31 right drag pans real camera without changing viewing direction and suppresses context menu', async () => {
  const { camera, canvas } = await viewer(); const start = camera.position.clone(); const direction = camera.getWorldDirection(start.clone())
  mouse(canvas,2,20,-10); expect(camera.position.equals(start)).toBe(false); expect(camera.position.y).toBeGreaterThan(start.y)
  expect(camera.getWorldDirection(start.clone()).dot(direction)).toBeCloseTo(1)
  const event = new MouseEvent('contextmenu', { bubbles: true, cancelable: true }); fireEvent(canvas,event); expect(event.defaultPrevented).toBe(true)
})
it('F02-2.8-31 wheel zoom changes camera radius, prevents page scroll and enforces orbit distance bounds', async () => {
  const { camera, canvas } = await viewer(); const radius = camera.position.length(); const event = new WheelEvent('wheel', { deltaY: 100, bubbles: true, cancelable: true }); fireEvent(canvas,event)
  expect(event.defaultPrevented).toBe(true); expect(camera.position.length()).toBeCloseTo(radius*1.1)
  for (let i=0;i<100;i++) fireEvent.wheel(canvas, { deltaY: 100 }); expect(camera.position.length()).toBeCloseTo(1000)
  for (let i=0;i<100;i++) fireEvent.wheel(canvas, { deltaY: -100 }); expect(camera.position.length()).toBeCloseTo(1)
})
it('F02-2.8-31 one-touch rotates and two-touch pinch zooms the actual camera', async () => {
  const { camera, canvas } = await viewer(); const start = camera.position.clone(); const radius = start.length()
  fireEvent.touchStart(canvas, { touches: [{ clientX: 100, clientY: 100 }] }); fireEvent.touchMove(canvas, { touches: [{ clientX: 120, clientY: 110 }] }); fireEvent.touchEnd(canvas)
  expect(camera.position.equals(start)).toBe(false); expect(camera.position.length()).toBeCloseTo(radius)
  fireEvent.touchStart(canvas, { touches: [{ clientX: 0, clientY: 0 },{ clientX: 100, clientY: 0 }] }); fireEvent.touchMove(canvas, { touches: [{ clientX: 0, clientY: 0 },{ clientX: 200, clientY: 0 }] }); fireEvent.touchEnd(canvas)
  expect(camera.position.length()).toBeCloseTo(radius/2)
})
it('F02-2.8-32 wireframe button mutates actual Three material and zoom buttons change camera radius', async () => {
  const { renderer, camera } = await viewer(); const mesh = renderer.scene.children.find((child: any) => child.isMesh); expect(mesh.material.wireframe).toBe(false)
  fireEvent.click(screen.getByRole('button', { name: 'Wireframe' })); expect(mesh.material.wireframe).toBe(true); fireEvent.click(screen.getByRole('button', { name: 'Wireframe' })); expect(mesh.material.wireframe).toBe(false)
  const radius = camera.position.length(); fireEvent.click(screen.getByRole('button', { name: 'Zoom in' })); expect(camera.position.length()).toBeCloseTo(radius*0.8)
  fireEvent.click(screen.getByRole('button', { name: 'Zoom out' })); expect(camera.position.length()).toBeCloseTo(radius)
})
it('F02-2.8-32 reset restores original orientation after rotation', async () => {
  const { camera, canvas } = await viewer(); const start = camera.position.clone(); mouse(canvas,0,20,10); fireEvent.click(screen.getByRole('button', { name: 'Reset view' }))
  expect(camera.position.distanceTo(start)).toBeCloseTo(0)
})
it('F02-2.8-32 CLAIM GAP: reset restores original framing distance after zoom and pan', async () => {
  const { camera, canvas } = await viewer(); const start = camera.position.clone(); fireEvent.click(screen.getByRole('button', { name: 'Zoom in' })); mouse(canvas,2,20,-10); fireEvent.click(screen.getByRole('button', { name: 'Reset view' }))
  expect(camera.position.distanceTo(start)).toBeCloseTo(0)
})
it('F02-2.8-31 unmount disposes renderer and detaches orbit listeners', async () => {
  const { unmount, renderer, camera, canvas } = await viewer(); const start = camera.position.clone(); unmount()
  expect(renderer.dispose).toHaveBeenCalledOnce(); expect(renderer.forceContextLoss).toHaveBeenCalledOnce(); expect(cancelAnimationFrame).toHaveBeenCalledWith(1)
  mouse(canvas,0,50,50); fireEvent.wheel(canvas, { deltaY: 100 }); expect(camera.position.toArray()).toEqual(start.toArray())
})
it('F02-2.8-34 CLAIM GAP: model library entry exposes a working 3D preview action', async () => {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false } } }); clients.push(c); render(<QueryClientProvider client={c}><MemoryRouter><Models /></MemoryRouter></QueryClientProvider>)
  await screen.findByRole('heading', { name: 'Fixture library model' }); const preview = screen.getByRole('button', { name: /(?:view|preview).*3d|3d.*(?:view|preview)/i }); fireEvent.click(preview)
  await screen.findByRole('dialog', { name: 'Fixture library model' }); expect(boundary.mesh).toHaveBeenCalledWith('/models/9/mesh')
})
