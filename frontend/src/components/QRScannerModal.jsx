import jsQR from 'jsqr';
import { useState, useEffect, useRef } from 'react';
import { QrCode, Camera, X, Check, Package, Printer } from 'lucide-react';
import { spools, printers } from '../api';
import { getColorName } from '../utils/colorNames';

/**
 * QR Scanner Modal
 * 
 * Opens camera to scan spool QR codes and assign them to printer slots.
 * Works on mobile (phone camera) and desktop (webcam).
 * 
 * Usage:
 *   <QRScannerModal 
 *     isOpen={showScanner} 
 *     onClose={() => setShowScanner(false)}
 *     onAssigned={(result) => console.log('Assigned:', result)}
 *   />
 */

export default function QRScannerModal({ isOpen, onClose, onAssigned, preselectedPrinter = null, preselectedSlot = null }) {
  const videoRef = useRef(null);
  const canvasRef = useRef(null);
  const streamRef = useRef(null);
  
  const [scanning, setScanning] = useState(false);
  const [scannedCode, setScannedCode] = useState(null);
  const [spoolData, setSpoolData] = useState(null);
  const [printerList, setPrinterList] = useState([]);
  const [selectedPrinter, setSelectedPrinter] = useState(preselectedPrinter);
  const [selectedSlot, setSelectedSlot] = useState(preselectedSlot);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);
  const [manualCode, setManualCode] = useState('');
  const [useManual, setUseManual] = useState(false);
  
  // Load printers on mount
  useEffect(() => {
    if (isOpen) {
      loadPrinters();
    }
  }, [isOpen]);
  
  // Start camera when modal opens
  useEffect(() => {
    if (isOpen && !useManual) {
      startCamera();
    }
    return () => stopCamera();
  }, [isOpen, useManual]);
  
  // Scan loop
  useEffect(() => {
    if (!scanning || !videoRef.current || !canvasRef.current) return;
    
    const interval = setInterval(() => {
      scanFrame();
    }, 200); // Scan 5 times per second
    
    return () => clearInterval(interval);
  }, [scanning]);
  
  const loadPrinters = async () => {
    try {
      const data = await printers.list();
      setPrinterList(data.filter(p => p.is_active));
    } catch (err) {
      console.error('Failed to load printers:', err);
    }
  };
  
  const startCamera = async () => {
    try {
      setError(null);
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: 'environment' } // Prefer back camera on mobile
      });
      streamRef.current = stream;
      if (videoRef.current) {
        videoRef.current.srcObject = stream;
        videoRef.current.play();
        setScanning(true);
      }
    } catch (err) {
      console.error('Camera error:', err);
      setError('Could not access camera. Use manual entry instead.');
      setUseManual(true);
    }
  };
  
  const stopCamera = () => {
    setScanning(false);
    if (streamRef.current) {
      streamRef.current.getTracks().forEach(track => track.stop());
      streamRef.current = null;
    }
  };
  
  const scanFrame = async () => {
    if (!videoRef.current || !canvasRef.current) return;
    
    const video = videoRef.current;
    const canvas = canvasRef.current;
    const ctx = canvas.getContext('2d');
    
    // Match canvas to video dimensions
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    ctx.drawImage(video, 0, 0);
    
    // Get image data for QR scanning
    const imageData = ctx.getImageData(0, 0, canvas.width, canvas.height);
    
    // Use jsQR library if available, otherwise we'll need to add it
    {
      const code = jsQR(imageData.data, imageData.width, imageData.height);
      if (code && code.data) {
        handleScannedCode(code.data);
      }
    }
  };
  
  const handleScannedCode = async (code) => {
    // Check if it looks like our spool code format
    if (!code.startsWith('SPL-')) {
      return; // Not a spool QR code
    }
    
    setScanning(false);
    stopCamera();
    setScannedCode(code);
    
    // Look up spool details
    try {
      const data = await spools.lookup(code);
      setSpoolData(data);
    } catch (err) {
      setError(`Spool not found: ${code}`);
      setSpoolData(null);
    }
  };
  
  const handleManualSubmit = () => {
    const code = manualCode.trim().toUpperCase();
    if (code) {
      handleScannedCode(code.startsWith('SPL-') ? code : `SPL-${code}`);
    }
  };
  
  const handleAssign = async () => {
    if (!scannedCode || selectedPrinter === null || selectedSlot === null) {
      setError('Please select a printer and slot');
      return;
    }
    
    try {
      setError(null);
      const result = await spools.scanAssign(scannedCode, selectedPrinter, selectedSlot);
      
      if (result.success) {
        setSuccess(result.message);
        if (onAssigned) {
          onAssigned(result);
        }
        // Auto-close after success
        setTimeout(() => {
          handleClose();
        }, 1500);
      } else {
        setError(result.message);
      }
    } catch (err) {
      setError('Failed to assign spool');
    }
  };
  
  const handleClose = () => {
    stopCamera();
    setScannedCode(null);
    setSpoolData(null);
    setSelectedPrinter(preselectedPrinter);
    setSelectedSlot(preselectedSlot);
    setError(null);
    setSuccess(null);
    setManualCode('');
    setUseManual(false);
    onClose();
  };
  
  const resetScan = () => {
    setScannedCode(null);
    setSpoolData(null);
    setError(null);
    setSuccess(null);
    if (!useManual) {
      startCamera();
    }
  };
  
  if (!isOpen) return null;
  
  const selectedPrinterData = printerList.find(p => p.id === selectedPrinter);
  
  return (
    <div className="fixed inset-0 bg-black/80 flex items-center justify-center z-50 p-4">
      <div className="bg-farm-900 rounded-xl border border-farm-700 w-full max-w-md">
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-farm-700">
          <div className="flex items-center gap-2">
            <QrCode className="w-5 h-5 text-print-500" />
            <h2 className="text-lg font-semibold">Scan Spool QR</h2>
          </div>
          <button onClick={handleClose} className="p-1 hover:bg-farm-800 rounded">
            <X className="w-5 h-5" />
          </button>
        </div>
        
        {/* Content */}
        <div className="p-4 space-y-4">
          {/* Success message */}
          {success && (
            <div className="bg-green-900/50 border border-green-700 rounded-lg p-4 flex items-center gap-3">
              <Check className="w-6 h-6 text-green-400" />
              <span className="text-green-300">{success}</span>
            </div>
          )}
          
          {/* Error message */}
          {error && (
            <div className="bg-red-900/50 border border-red-700 rounded-lg p-3 text-red-300 text-sm">
              {error}
            </div>
          )}
          
          {/* Camera / Scanner view */}
          {!scannedCode && !success && (
            <>
              {!useManual ? (
                <div className="relative">
                  <video
                    ref={videoRef}
                    className="w-full rounded-lg bg-black aspect-square object-cover"
                    playsInline
                    muted
                  />
                  <canvas ref={canvasRef} className="hidden" />
                  
                  {/* Scanning overlay */}
                  <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
                    <div className="w-48 h-48 border-2 border-print-500 rounded-lg opacity-50" />
                  </div>
                  
                  {scanning && (
                    <div className="absolute bottom-2 left-1/2 -translate-x-1/2 bg-black/70 px-3 py-1 rounded-full text-sm">
                      Scanning...
                    </div>
                  )}
                </div>
              ) : (
                <div className="space-y-3">
                  <label className="block text-sm text-farm-400">Enter spool code manually:</label>
                  <div className="flex gap-2">
                    <input
                      type="text"
                      value={manualCode}
                      onChange={(e) => setManualCode(e.target.value)}
                      placeholder="SPL-XXXXXXXX"
                      className="flex-1 bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm"
                      onKeyDown={(e) => e.key === 'Enter' && handleManualSubmit()}
                    />
                    <button
                      onClick={handleManualSubmit}
                      className="px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg text-sm"
                    >
                      Look Up
                    </button>
                  </div>
                </div>
              )}
              
              <button
                onClick={() => setUseManual(!useManual)}
                className="w-full text-center text-sm text-farm-400 hover:text-farm-300"
              >
                {useManual ? '← Use camera instead' : 'Enter code manually →'}
              </button>
            </>
          )}
          
          {/* Scanned spool info */}
          {scannedCode && spoolData && !success && (
            <div className="space-y-4">
              <div className="bg-farm-800 rounded-lg p-4">
                <div className="flex items-center gap-3">
                  <div
                    className="w-10 h-10 rounded-lg border border-farm-600"
                    style={{ backgroundColor: spoolData.filament_color_hex || '#666' }}
                  />
                  <div>
                    <div className="font-medium">{spoolData.filament_name}{spoolData.filament_color_hex && <span className="text-farm-400 ml-2">({getColorName(spoolData.filament_color_hex)})</span>}</div>
                    <div className="text-sm text-farm-400">
                      {spoolData.filament_brand} {spoolData.filament_material} • {spoolData.remaining_weight_g || spoolData.initial_weight_g}g
                    </div>
                  </div>
                </div>
                <div className="mt-2 text-xs text-farm-500 font-mono">{scannedCode}</div>
              </div>
              
              {/* Printer selection */}
              <div>
                <label className="block text-sm text-farm-400 mb-2">Assign to printer:</label>
                <select
                  value={selectedPrinter || ''}
                  onChange={(e) => {
                    setSelectedPrinter(parseInt(e.target.value));
                    setSelectedSlot(null);
                  }}
                  className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm"
                >
                  <option value="">Select printer...</option>
                  {printerList.map(p => (
                    <option key={p.id} value={p.id}>{p.name}</option>
                  ))}
                </select>
              </div>
              
              {/* Slot selection */}
              {selectedPrinter && selectedPrinterData && (
                <div>
                  <label className="block text-sm text-farm-400 mb-2">Select slot:</label>
                  <div className="grid grid-cols-4 gap-2">
                    {Array.from({ length: selectedPrinterData.slot_count || 4 }, (_, i) => (
                      <button
                        key={i}
                        onClick={() => setSelectedSlot(i + 1)}
                        className={`p-3 rounded-lg border text-sm font-medium transition-colors ${
                          selectedSlot === i + 1
                            ? 'bg-print-600 border-print-500 text-white'
                            : 'bg-farm-800 border-farm-700 hover:border-farm-600'
                        }`}
                      >
                        {i + 1}
                      </button>
                    ))}
                  </div>
                </div>
              )}
              
              {/* Actions */}
              <div className="flex gap-2">
                <button
                  onClick={resetScan}
                  className="flex-1 px-4 py-2 bg-farm-800 hover:bg-farm-700 rounded-lg text-sm"
                >
                  Scan Again
                </button>
                <button
                  onClick={handleAssign}
                  disabled={selectedPrinter === null || selectedSlot === null}
                  className="flex-1 px-4 py-2 bg-print-600 hover:bg-print-500 disabled:opacity-50 disabled:cursor-not-allowed rounded-lg text-sm"
                >
                  Assign to Slot
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
