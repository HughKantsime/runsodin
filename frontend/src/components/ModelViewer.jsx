import { useState, useEffect, useRef, useCallback } from 'react'
import { X, RotateCcw, ZoomIn, ZoomOut, Maximize2, Box } from 'lucide-react'
import { fetchAPI } from '../api'

/**
 * 3D Model Viewer using Three.js
 * Renders mesh geometry extracted from .3mf files
 * Opens as a modal overlay from model cards
 */

// Lazy-load Three.js from CDN
let THREE = null
let OrbitControlsFactory = null

async function loadThreeJS() {
  if (THREE) return
  
  // Import Three.js from CDN via dynamic import
  const threeModule = await import('https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.module.js')
  THREE = threeModule
}

export default function ModelViewer({ modelId, modelName, onClose }) {
  const containerRef = useRef(null)
  const rendererRef = useRef(null)
  const sceneRef = useRef(null)
  const cameraRef = useRef(null)
  const controlsRef = useRef(null)
  const frameRef = useRef(null)
  const meshRef = useRef(null)
  
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [meshInfo, setMeshInfo] = useState(null)
  const [wireframe, setWireframe] = useState(false)

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (frameRef.current) cancelAnimationFrame(frameRef.current)
      if (rendererRef.current) {
        rendererRef.current.dispose()
        rendererRef.current.forceContextLoss()
      }
      if (controlsRef.current) controlsRef.current.dispose()
    }
  }, [])

  // Simple orbit controls (no import needed)
  const setupControls = useCallback((camera, domElement) => {
    let isDown = false
    let isRightDown = false
    let prevX = 0, prevY = 0
    let theta = Math.PI / 4, phi = Math.PI / 3
    let radius = camera.position.length()
    const target = new THREE.Vector3(0, 0, 0)
    
    const updateCamera = () => {
      camera.position.x = target.x + radius * Math.sin(phi) * Math.cos(theta)
      camera.position.y = target.y + radius * Math.cos(phi)
      camera.position.z = target.z + radius * Math.sin(phi) * Math.sin(theta)
      camera.lookAt(target)
    }
    
    const onMouseDown = (e) => {
      if (e.button === 0) { isDown = true }
      if (e.button === 2) { isRightDown = true }
      prevX = e.clientX
      prevY = e.clientY
    }
    
    const onMouseUp = () => { isDown = false; isRightDown = false }
    
    const onMouseMove = (e) => {
      const dx = e.clientX - prevX
      const dy = e.clientY - prevY
      prevX = e.clientX
      prevY = e.clientY
      
      if (isDown) {
        // Rotate
        theta -= dx * 0.01
        phi -= dy * 0.01
        phi = Math.max(0.1, Math.min(Math.PI - 0.1, phi))
        updateCamera()
      }
      if (isRightDown) {
        // Pan
        const panSpeed = radius * 0.002
        const right = new THREE.Vector3()
        const up = new THREE.Vector3()
        camera.getWorldDirection(new THREE.Vector3())
        right.crossVectors(camera.up, new THREE.Vector3().subVectors(camera.position, target)).normalize()
        up.copy(camera.up)
        target.add(right.multiplyScalar(dx * panSpeed))
        target.add(up.multiplyScalar(-dy * panSpeed))
        updateCamera()
      }
    }
    
    const onWheel = (e) => {
      e.preventDefault()
      radius *= e.deltaY > 0 ? 1.1 : 0.9
      radius = Math.max(1, Math.min(1000, radius))
      updateCamera()
    }
    
    domElement.addEventListener('mousedown', onMouseDown)
    domElement.addEventListener('mouseup', onMouseUp)
    domElement.addEventListener('mousemove', onMouseMove)
    domElement.addEventListener('wheel', onWheel, { passive: false })
    domElement.addEventListener('contextmenu', (e) => e.preventDefault())
    
    // Touch support
    let lastTouchDist = 0
    const onTouchStart = (e) => {
      if (e.touches.length === 1) {
        isDown = true
        prevX = e.touches[0].clientX
        prevY = e.touches[0].clientY
      } else if (e.touches.length === 2) {
        lastTouchDist = Math.hypot(
          e.touches[0].clientX - e.touches[1].clientX,
          e.touches[0].clientY - e.touches[1].clientY
        )
      }
    }
    const onTouchMove = (e) => {
      e.preventDefault()
      if (e.touches.length === 1 && isDown) {
        const dx = e.touches[0].clientX - prevX
        const dy = e.touches[0].clientY - prevY
        prevX = e.touches[0].clientX
        prevY = e.touches[0].clientY
        theta -= dx * 0.01
        phi -= dy * 0.01
        phi = Math.max(0.1, Math.min(Math.PI - 0.1, phi))
        updateCamera()
      } else if (e.touches.length === 2) {
        const dist = Math.hypot(
          e.touches[0].clientX - e.touches[1].clientX,
          e.touches[0].clientY - e.touches[1].clientY
        )
        radius *= lastTouchDist / dist
        radius = Math.max(1, Math.min(1000, radius))
        lastTouchDist = dist
        updateCamera()
      }
    }
    const onTouchEnd = () => { isDown = false }
    
    domElement.addEventListener('touchstart', onTouchStart, { passive: false })
    domElement.addEventListener('touchmove', onTouchMove, { passive: false })
    domElement.addEventListener('touchend', onTouchEnd)
    
    updateCamera()
    
    return {
      dispose: () => {
        domElement.removeEventListener('mousedown', onMouseDown)
        domElement.removeEventListener('mouseup', onMouseUp)
        domElement.removeEventListener('mousemove', onMouseMove)
        domElement.removeEventListener('wheel', onWheel)
        domElement.removeEventListener('touchstart', onTouchStart)
        domElement.removeEventListener('touchmove', onTouchMove)
        domElement.removeEventListener('touchend', onTouchEnd)
      },
      reset: () => {
        theta = Math.PI / 4
        phi = Math.PI / 3
        radius = camera.position.length()
        target.set(0, 0, 0)
        updateCamera()
      },
      zoomIn: () => { radius *= 0.8; updateCamera() },
      zoomOut: () => { radius *= 1.25; updateCamera() }
    }
  }, [])

  // Initialize scene and load mesh
  useEffect(() => {
    if (!containerRef.current || !modelId) return
    
    let cancelled = false
    
    async function init() {
      try {
        // Load Three.js
        await loadThreeJS()
        if (cancelled) return
        
        // Fetch mesh data
        const meshData = await fetchAPI(`/models/${modelId}/mesh`)
        if (cancelled) return
        
        if (!meshData || !meshData.vertices || !meshData.triangles) {
          setError('No 3D data available for this model')
          setLoading(false)
          return
        }
        
        setMeshInfo({
          vertices: meshData.vertex_count,
          triangles: meshData.triangle_count
        })
        
        const container = containerRef.current
        const width = container.clientWidth
        const height = container.clientHeight
        
        // Scene
        const scene = new THREE.Scene()
        scene.background = new THREE.Color(0x1a1a2e)
        sceneRef.current = scene
        
        // Camera
        const camera = new THREE.PerspectiveCamera(45, width / height, 0.1, 10000)
        cameraRef.current = camera
        
        // Renderer
        const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true })
        renderer.setSize(width, height)
        renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
        renderer.shadowMap.enabled = true
        container.appendChild(renderer.domElement)
        rendererRef.current = renderer
        
        // Build geometry from mesh data
        const geometry = new THREE.BufferGeometry()
        const vertices = new Float32Array(meshData.vertices)
        const indices = new Uint32Array(meshData.triangles)
        
        geometry.setAttribute('position', new THREE.BufferAttribute(vertices, 3))
        geometry.setIndex(new THREE.BufferAttribute(indices, 1))
        geometry.computeVertexNormals()
        
        // Center and scale the model
        geometry.computeBoundingBox()
        const box = geometry.boundingBox
        const center = new THREE.Vector3()
        box.getCenter(center)
        geometry.translate(-center.x, -center.y, -center.z)
        
        const size = new THREE.Vector3()
        box.getSize(size)
        const maxDim = Math.max(size.x, size.y, size.z)
        const scale = 100 / maxDim  // normalize to ~100 units
        geometry.scale(scale, scale, scale)
        
        // Material — industrial look matching O.D.I.N. theme
        const material = new THREE.MeshPhongMaterial({
          color: 0xd4a843,       // amber/gold — matches O.D.I.N. accent
          specular: 0x444444,
          shininess: 30,
          flatShading: false,
          side: THREE.DoubleSide
        })
        
        const mesh = new THREE.Mesh(geometry, material)
        mesh.castShadow = true
        mesh.receiveShadow = true
        scene.add(mesh)
        meshRef.current = mesh
        
        // Grid floor
        const gridHelper = new THREE.GridHelper(200, 20, 0x333355, 0x222244)
        gridHelper.position.y = -size.y * scale / 2
        scene.add(gridHelper)
        
        // Lighting
        const ambientLight = new THREE.AmbientLight(0x404060, 0.6)
        scene.add(ambientLight)
        
        const dirLight = new THREE.DirectionalLight(0xffffff, 0.8)
        dirLight.position.set(100, 150, 100)
        dirLight.castShadow = true
        scene.add(dirLight)
        
        const dirLight2 = new THREE.DirectionalLight(0x8888ff, 0.3)
        dirLight2.position.set(-100, 50, -100)
        scene.add(dirLight2)
        
        const rimLight = new THREE.DirectionalLight(0xd4a843, 0.2)
        rimLight.position.set(0, -50, -100)
        scene.add(rimLight)
        
        // Position camera
        camera.position.set(80, 60, 80)
        camera.lookAt(0, 0, 0)
        
        // Controls
        const controls = setupControls(camera, renderer.domElement)
        controlsRef.current = controls
        
        // Animation loop
        const animate = () => {
          frameRef.current = requestAnimationFrame(animate)
          renderer.render(scene, camera)
        }
        animate()
        
        // Handle resize
        const onResize = () => {
          const w = container.clientWidth
          const h = container.clientHeight
          camera.aspect = w / h
          camera.updateProjectionMatrix()
          renderer.setSize(w, h)
        }
        window.addEventListener('resize', onResize)
        
        setLoading(false)
        
      } catch (err) {
        console.error('3D viewer error:', err)
        if (!cancelled) {
          setError(err.message || 'Failed to load 3D model')
          setLoading(false)
        }
      }
    }
    
    init()
    
    return () => { cancelled = true }
  }, [modelId, setupControls])

  // Toggle wireframe
  useEffect(() => {
    if (meshRef.current) {
      meshRef.current.material.wireframe = wireframe
    }
  }, [wireframe])

  // Close on Escape
  useEffect(() => {
    const handleKey = (e) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [onClose])

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm" role="dialog" aria-modal="true" aria-labelledby="model-viewer-title">
      <div className="relative w-[90vw] h-[80vh] max-w-6xl bg-farm-900 rounded-lg border border-farm-700 overflow-hidden flex flex-col">

        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-farm-700 bg-farm-900/80">
          <div className="flex items-center gap-3">
            <Box size={18} className="text-amber-400" aria-hidden="true" />
            <h3 id="model-viewer-title" className="font-medium text-white">{modelName || '3D Preview'}</h3>
            {meshInfo && (
              <span className="text-xs text-farm-400">
                {meshInfo.vertices.toLocaleString()} vertices · {meshInfo.triangles.toLocaleString()} triangles
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            {/* Wireframe toggle */}
            <button
              onClick={() => setWireframe(!wireframe)}
              className={`px-2 py-1 text-xs rounded-lg ${wireframe ? 'bg-amber-500/20 text-amber-400 border border-amber-500/30' : 'bg-farm-700 text-farm-300 border border-farm-600'}`}
              title="Toggle wireframe"
            >
              Wireframe
            </button>
            {/* Zoom controls */}
            <button
              onClick={() => controlsRef.current?.zoomIn()}
              className="p-1.5 bg-farm-700 hover:bg-farm-600 rounded-lg text-farm-300"
              aria-label="Zoom in"
            >
              <ZoomIn size={14} />
            </button>
            <button
              onClick={() => controlsRef.current?.zoomOut()}
              className="p-1.5 bg-farm-700 hover:bg-farm-600 rounded-lg text-farm-300"
              aria-label="Zoom out"
            >
              <ZoomOut size={14} />
            </button>
            {/* Reset view */}
            <button
              onClick={() => controlsRef.current?.reset()}
              className="p-1.5 bg-farm-700 hover:bg-farm-600 rounded-lg text-farm-300"
              aria-label="Reset view"
            >
              <RotateCcw size={14} />
            </button>
            {/* Close */}
            <button
              onClick={onClose}
              className="p-1.5 bg-farm-700 hover:bg-red-600 rounded-lg text-farm-300"
              aria-label="Close 3D preview"
            >
              <X size={14} />
            </button>
          </div>
        </div>
        
        {/* Viewer area */}
        <div ref={containerRef} className="flex-1 relative cursor-grab active:cursor-grabbing">
          {loading && (
            <div className="absolute inset-0 flex items-center justify-center">
              <div className="text-center">
                <div className="animate-spin w-8 h-8 border-2 border-amber-400 border-t-transparent rounded-full mx-auto mb-3" />
                <p className="text-farm-400 text-sm">Loading 3D model...</p>
              </div>
            </div>
          )}
          {error && (
            <div className="absolute inset-0 flex items-center justify-center">
              <div className="text-center">
                <Box size={48} className="text-farm-500 mx-auto mb-3" />
                <p className="text-farm-400 text-sm">{error}</p>
                <p className="text-farm-500 text-xs mt-1">3D preview requires a re-upload of the .3mf file</p>
              </div>
            </div>
          )}
        </div>
        
        {/* Footer hint */}
        <div className="px-4 py-2 border-t border-farm-700 bg-farm-900/80">
          <p className="text-xs text-farm-500">
            Left-click drag to rotate · Right-click drag to pan · Scroll to zoom
          </p>
        </div>
      </div>
    </div>
  )
}
