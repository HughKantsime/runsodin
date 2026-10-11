import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import useCameraList, { CameraListError, useCameraScope } from '../../hooks/useCameraList'
import type { Camera } from '../../types/printer'
import CameraPipPlayer from './CameraPipPlayer'
const CameraPipContext = createContext({ open: (_camera: Camera) => {}, close: () => {} })
export const useCameraPip = () => useContext(CameraPipContext)
function ScopedPlayer({ children }: { children: ReactNode }) {
  const query = useCameraList()
  const [selected, setSelected] = useState<Camera | null>(null)
  const found = query.data?.find(camera => camera.id === selected?.id && camera.camera_enabled !== false)
  const accessDenied = query.error instanceof CameraListError && [401, 403].includes(query.error.status)
  const invalid = !query.ready || accessDenied || (!query.isError && query.isSuccess && !found)
  useEffect(() => { if (invalid) setSelected(null) }, [invalid])
  return <CameraPipContext.Provider value={{
    open: camera => { const current = query.data?.find(item => item.id === camera.id && item.camera_enabled !== false); if (query.ready && !accessDenied && current) setSelected(current) },
    close: () => setSelected(null),
  }}>
    {children}
    {selected && !invalid && <CameraPipPlayer key={selected.id} camera={found || selected} onClose={() => setSelected(null)} warning={query.isError} />}
  </CameraPipContext.Provider>
}
export default function CameraPipProvider({ children }: { children: ReactNode }) {
  const { identity } = useCameraScope()
  return <ScopedPlayer key={identity}>{children}</ScopedPlayer>
}
