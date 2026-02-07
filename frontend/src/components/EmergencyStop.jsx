import { useState, useEffect } from 'react';
import { StopCircle, Pause, Play, X, AlertTriangle } from 'lucide-react';

const API_BASE = '/api';
const API_KEY = import.meta.env.VITE_API_KEY;

/**
 * Emergency Stop Button - Floating button that shows when any printer is actively printing.
 * Provides quick access to stop, pause, or resume prints.
 */
export default function EmergencyStop() {
  const [printers, setPrinters] = useState([]);
  const [showPanel, setShowPanel] = useState(false);
  const [loading, setLoading] = useState({});
  const [confirmStop, setConfirmStop] = useState(null);

  // Fetch printer status
  useEffect(() => {
    const fetchPrinters = async () => {
      try {
        const token = localStorage.getItem('token');
        const headers = { 'X-API-Key': API_KEY };
        if (token) headers['Authorization'] = `Bearer ${token}`;
        
        const res = await fetch(`${API_BASE}/printers`, { headers });
        if (res.ok) {
          const data = await res.json();
          setPrinters(data.filter(p => 
            p.gcode_state === 'RUNNING' || 
            p.gcode_state === 'PRINTING' || 
            p.gcode_state === 'PAUSED' ||
            p.gcode_state === 'PAUSE'
          ));
        }
      } catch (err) {
        console.error('Failed to fetch printers:', err);
      }
    };

    fetchPrinters();
    const interval = setInterval(fetchPrinters, 5000);
    return () => clearInterval(interval);
  }, []);

  const sendCommand = async (printerId, action) => {
    setLoading(prev => ({ ...prev, [printerId]: action }));
    
    try {
      const token = localStorage.getItem('token');
      const headers = { 'X-API-Key': API_KEY };
      if (token) headers['Authorization'] = `Bearer ${token}`;
      
      const res = await fetch(`${API_BASE}/printers/${printerId}/${action}`, {
        method: 'POST',
        headers
      });
      
      if (!res.ok) {
        const err = await res.json();
        alert(`Failed: ${err.detail || 'Unknown error'}`);
      }
    } catch (err) {
      alert(`Error: ${err.message}`);
    } finally {
      setLoading(prev => ({ ...prev, [printerId]: null }));
      setConfirmStop(null);
    }
  };

  // Don't show if no active prints
  if (printers.length === 0) return null;

  const activePrinting = printers.filter(p => 
    p.gcode_state === 'RUNNING' || p.gcode_state === 'PRINTING'
  ).length;

  return (
    <>
      {/* Floating Button */}
      <button
        onClick={() => setShowPanel(!showPanel)}
        className={`fixed bottom-6 right-6 z-40 flex items-center gap-2 px-4 py-3 rounded-full shadow-lg transition-all ${
          showPanel
            ? 'bg-farm-800 text-white'
            : 'bg-red-600 hover:bg-red-500 text-white animate-pulse'
        }`}
        title="Emergency Controls"
      >
        {showPanel ? (
          <X size={20} />
        ) : (
          <>
            <StopCircle size={20} />
            <span className="font-medium">{activePrinting} Active</span>
          </>
        )}
      </button>

      {/* Control Panel */}
      {showPanel && (
        <div className="fixed bottom-20 right-6 z-40 w-80 bg-farm-900 border border-farm-700 rounded-xl shadow-2xl overflow-hidden">
          <div className="p-3 bg-red-900/50 border-b border-red-800 flex items-center gap-2">
            <AlertTriangle className="text-red-400" size={18} />
            <span className="font-medium text-red-200">Emergency Controls</span>
          </div>
          
          <div className="p-3 space-y-2 max-h-80 overflow-y-auto">
            {printers.map(printer => {
              const isPaused = printer.gcode_state === 'PAUSED' || printer.gcode_state === 'PAUSE';
              const isLoading = loading[printer.id];
              
              return (
                <div key={printer.id} className="p-3 bg-farm-800 rounded-lg">
                  <div className="flex items-center justify-between mb-2">
                    <span className="font-medium text-sm">{printer.nickname || printer.name}</span>
                    <span className={`text-xs px-2 py-0.5 rounded ${
                      isPaused ? 'bg-yellow-900/50 text-yellow-400' : 'bg-green-900/50 text-green-400'
                    }`}>
                      {isPaused ? 'Paused' : 'Printing'}
                    </span>
                  </div>
                  
                  <div className="flex gap-2">
                    {isPaused ? (
                      <button
                        onClick={() => sendCommand(printer.id, 'resume')}
                        disabled={isLoading}
                        className="flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 bg-green-700 hover:bg-green-600 rounded text-sm transition-colors disabled:opacity-50"
                      >
                        <Play size={14} />
                        {isLoading === 'resume' ? 'Resuming...' : 'Resume'}
                      </button>
                    ) : (
                      <button
                        onClick={() => sendCommand(printer.id, 'pause')}
                        disabled={isLoading}
                        className="flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 bg-yellow-700 hover:bg-yellow-600 rounded text-sm transition-colors disabled:opacity-50"
                      >
                        <Pause size={14} />
                        {isLoading === 'pause' ? 'Pausing...' : 'Pause'}
                      </button>
                    )}
                    
                    {confirmStop === printer.id ? (
                      <button
                        onClick={() => sendCommand(printer.id, 'stop')}
                        disabled={isLoading}
                        className="flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 bg-red-600 hover:bg-red-500 rounded text-sm transition-colors disabled:opacity-50 animate-pulse"
                      >
                        <StopCircle size={14} />
                        {isLoading === 'stop' ? 'Stopping...' : 'Confirm Stop'}
                      </button>
                    ) : (
                      <button
                        onClick={() => setConfirmStop(printer.id)}
                        disabled={isLoading}
                        className="flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 bg-red-900 hover:bg-red-800 rounded text-sm transition-colors disabled:opacity-50"
                      >
                        <StopCircle size={14} />
                        Stop
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
          
          <div className="p-2 bg-farm-950 text-xs text-farm-500 text-center">
            Stop will cancel the print permanently
          </div>
        </div>
      )}
    </>
  );
}
