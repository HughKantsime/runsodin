import { useState, useRef, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { 
  ChevronLeft, 
  ChevronRight, 
  ZoomIn, 
  ZoomOut,
  Calendar as CalendarIcon,
  GripVertical
} from 'lucide-react'
import { format, addDays, startOfDay, differenceInMinutes, isSameDay } from 'date-fns'
import clsx from 'clsx'

import { timeline, printers, jobActions } from '../api'

// Parse date string as UTC (backend returns without Z)
function parseUTC(dateStr) {
  if (!dateStr) return null
  // If no timezone indicator, assume UTC
  if (!dateStr.endsWith('Z') && !dateStr.includes('+')) {
    dateStr = dateStr + 'Z'
  }
  return new Date(dateStr)
}

const SLOT_MINUTES = 15
const ROW_HEIGHT = 56
const PRINTER_COL_WIDTH = 120

const statusColors = {
  pending: 'bg-status-pending',
  scheduled: 'bg-status-scheduled',
  printing: 'bg-status-printing',
  completed: 'bg-status-completed',
  failed: 'bg-status-failed',
}

function TimelineHeader({ startDate, days, slotWidth }) {
  const slots = []
  const totalSlots = days * (24 * 60 / SLOT_MINUTES)

  for (let i = 0; i < totalSlots; i++) {
    const slotTime = new Date(startDate.getTime() + i * SLOT_MINUTES * 60 * 1000)
    const isHourMark = slotTime.getMinutes() === 0
    const isDayStart = slotTime.getHours() === 0 && slotTime.getMinutes() === 0
    
    slots.push(
      <div
        key={i}
        className={clsx(
          'flex-shrink-0 border-r border-farm-800 text-center text-xs',
          isDayStart && 'border-l-2 border-l-farm-600'
        )}
        style={{ width: slotWidth }}
      >
        {isHourMark && (
          <span className="text-farm-500">
            {format(slotTime, 'HH:mm')}
          </span>
        )}
      </div>
    )
  }

  return (
    <div className="flex bg-farm-900 border-b border-farm-700 sticky top-0 z-10">
      <div 
        className="flex-shrink-0 bg-farm-900 border-r border-farm-700 px-3 py-2 font-medium"
        style={{ width: PRINTER_COL_WIDTH }}
      >
        Printer
      </div>
      <div className="flex overflow-hidden">
        {slots}
      </div>
    </div>
  )
}

function TimelineDateHeader({ startDate, days, slotWidth }) {
  const dayHeaders = []
  const slotsPerDay = 24 * 60 / SLOT_MINUTES
  
  for (let d = 0; d < days; d++) {
    const day = addDays(startDate, d)
    const isToday = isSameDay(day, new Date())
    
    dayHeaders.push(
      <div
        key={d}
        className={clsx(
          'flex-shrink-0 text-center py-1 border-r border-farm-700',
          isToday ? 'bg-print-900/30 text-print-400' : 'text-farm-400'
        )}
        style={{ width: slotWidth * slotsPerDay }}
      >
        <span className="font-medium">{format(day, 'EEE')}</span>
        <span className="ml-2 text-farm-500">{format(day, 'MMM d')}</span>
      </div>
    )
  }

  return (
    <div className="flex bg-farm-950 border-b border-farm-800">
      <div 
        className="flex-shrink-0 border-r border-farm-700"
        style={{ width: PRINTER_COL_WIDTH }}
      />
      <div className="flex">
        {dayHeaders}
      </div>
    </div>
  )
}

function NowIndicator({ startDate, slotWidth }) {
  const now = new Date()
  const minutesFromStart = differenceInMinutes(now, startDate)
  
  if (minutesFromStart < 0) return null
  
  const left = PRINTER_COL_WIDTH + (minutesFromStart / SLOT_MINUTES) * slotWidth

  return (
    <div
      className="absolute top-0 bottom-0 w-0.5 bg-red-500 z-20 pointer-events-none"
      style={{ left }}
    >
      <div className="absolute -top-1 -left-1 w-2 h-2 bg-red-500 rounded-full" />
    </div>
  )
}

export default function Timeline() {
  const queryClient = useQueryClient()
  const containerRef = useRef(null)
  const [startDate, setStartDate] = useState(() => startOfDay(new Date()))
  const [days, setDays] = useState(7)
  const [slotWidth, setSlotWidth] = useState(20)
  
  // All drag state in one ref to avoid closure issues
  const dragRef = useRef({
    active: false,
    job: null,
    printerId: null,
    slotIndex: null,
    newStartTime: null
  })
  
  // Just for UI updates
  const [dragUI, setDragUI] = useState({ active: false, printerId: null, left: 0, width: 0, label: '', jobId: null })

  const { data: printersData } = useQuery({
    queryKey: ['printers'],
    queryFn: () => printers.list(true),
  })

  const { data: timelineData, isLoading } = useQuery({
    queryKey: ['timeline', startDate.toISOString(), days],
    queryFn: () => timeline.get(startDate.toISOString(), days),
  })

  const moveJobMutation = useMutation({
    mutationFn: ({ jobId, printerId, scheduledStart }) => 
      jobActions.move(jobId, printerId, scheduledStart),
    onSuccess: () => {
      queryClient.invalidateQueries(['timeline'])
      queryClient.invalidateQueries(['jobs'])
    },
    onError: (error) => {
      alert('Move failed: ' + (error?.message || 'Unknown error'))
    }
  })

  // Set up drag handlers once
  useEffect(() => {
    const handleMouseMove = (e) => {
      if (!dragRef.current.active || !containerRef.current) return

      const gridElements = containerRef.current.querySelectorAll('.timeline-grid')
      let targetPrinterId = null
      let targetRect = null

      gridElements.forEach((el) => {
        const rect = el.getBoundingClientRect()
        if (e.clientY >= rect.top && e.clientY <= rect.bottom) {
          targetPrinterId = parseInt(el.dataset.printerId)
          targetRect = rect
        }
      })

      if (targetPrinterId && targetRect) {
        const x = e.clientX - targetRect.left
        const currentSlotWidth = slotWidth
        const idx = Math.max(0, Math.floor(x / currentSlotWidth))
        
        const job = dragRef.current.job
        const durationMinutes = differenceInMinutes(parseUTC(job.end), parseUTC(job.start))
        const durationSlots = Math.ceil(durationMinutes / SLOT_MINUTES)
        
        const newTime = new Date(startDate.getTime() + idx * SLOT_MINUTES * 60 * 1000)
        
        // Store in ref
        dragRef.current.printerId = targetPrinterId
        dragRef.current.slotIndex = idx
        dragRef.current.newStartTime = newTime
        
        // Update UI
        setDragUI({
          active: true,
          printerId: targetPrinterId,
          left: idx * currentSlotWidth,
          width: durationSlots * currentSlotWidth - 2,
          label: format(newTime, 'MMM d HH:mm'),
          jobId: job.job_id
        })
      }
    }

    const handleMouseUp = () => {
      if (!dragRef.current.active) return
      
      const { job, printerId, newStartTime } = dragRef.current
      
      if (job && printerId && newStartTime) {
        const isoTime = newStartTime.toISOString()
        moveJobMutation.mutate({
          jobId: job.job_id,
          printerId: printerId,
          scheduledStart: isoTime
        })
      }
      
      // Reset
      dragRef.current = { active: false, job: null, printerId: null, slotIndex: null, newStartTime: null }
      setDragUI({ active: false, printerId: null, left: 0, width: 0, label: '', jobId: null })
    }

    window.addEventListener('mousemove', handleMouseMove)
    window.addEventListener('mouseup', handleMouseUp)
    
    return () => {
      window.removeEventListener('mousemove', handleMouseMove)
      window.removeEventListener('mouseup', handleMouseUp)
    }
  }, [startDate, slotWidth, moveJobMutation])

  const handleJobMouseDown = (e, job) => {
    e.preventDefault()
    dragRef.current = {
      active: true,
      job: job,
      printerId: null,
      slotIndex: null,
      newStartTime: null
    }
    setDragUI(prev => ({ ...prev, active: true, jobId: job.job_id }))
  }

  const goToPrevWeek = () => setStartDate(d => addDays(d, -7))
  const goToNextWeek = () => setStartDate(d => addDays(d, 7))
  const goToToday = () => setStartDate(startOfDay(new Date()))

  const slotsByPrinter = {}
  timelineData?.slots?.forEach((slot) => {
    if (!slotsByPrinter[slot.printer_id]) {
      slotsByPrinter[slot.printer_id] = []
    }
    slotsByPrinter[slot.printer_id].push(slot)
  })

  const totalSlots = days * (24 * 60 / SLOT_MINUTES)

  return (
    <div 
      className={clsx("h-full flex flex-col", dragUI.active && "cursor-grabbing select-none")}
      ref={containerRef}
    >
      <div className="flex items-center justify-between p-4 border-b border-farm-800 bg-farm-950">
        <div className="flex items-center gap-2">
          <button onClick={goToPrevWeek} className="p-2 bg-farm-800 hover:bg-farm-700 rounded-lg">
            <ChevronLeft size={20} />
          </button>
          <button onClick={goToToday} className="px-4 py-2 bg-farm-800 hover:bg-farm-700 rounded-lg flex items-center gap-2">
            <CalendarIcon size={16} />
            Today
          </button>
          <button onClick={goToNextWeek} className="p-2 bg-farm-800 hover:bg-farm-700 rounded-lg">
            <ChevronRight size={20} />
          </button>
          <span className="ml-4 text-farm-400">
            {format(startDate, 'MMM d')} — {format(addDays(startDate, days - 1), 'MMM d, yyyy')}
          </span>
        </div>

        <div className="flex items-center gap-2">
          {dragUI.active && <span className="text-sm text-print-400 mr-4">Drop at: {dragUI.label}</span>}
          <button onClick={() => setSlotWidth(w => Math.max(10, w - 5))} className="p-2 bg-farm-800 hover:bg-farm-700 rounded-lg">
            <ZoomOut size={18} />
          </button>
          <button onClick={() => setSlotWidth(w => Math.min(60, w + 5))} className="p-2 bg-farm-800 hover:bg-farm-700 rounded-lg">
            <ZoomIn size={18} />
          </button>
          <select value={days} onChange={(e) => setDays(Number(e.target.value))} className="bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm">
            <option value={3}>3 days</option>
            <option value={7}>1 week</option>
            <option value={14}>2 weeks</option>
          </select>
        </div>
      </div>

      <div className="flex-1 overflow-auto relative">
        {isLoading ? (
          <div className="flex items-center justify-center h-64 text-farm-500">Loading...</div>
        ) : (
          <div className="min-w-max">
            <TimelineDateHeader startDate={startDate} days={days} slotWidth={slotWidth} />
            <TimelineHeader startDate={startDate} days={days} slotWidth={slotWidth} />
            
            <div className="relative">
              <NowIndicator startDate={startDate} slotWidth={slotWidth} />
              
              {printersData?.map((printer) => {
                const slots = slotsByPrinter[printer.id] || []
                const jobBlocks = slots.map((slot) => {
                  const startMinutes = differenceInMinutes(parseUTC(slot.start), startDate)
                  const endMinutes = differenceInMinutes(parseUTC(slot.end), startDate)
                  const startSlot = Math.floor(startMinutes / SLOT_MINUTES)
                  const duration = Math.ceil((endMinutes - startMinutes) / SLOT_MINUTES)
                  return {
                    ...slot,
                    left: startSlot * slotWidth,
                    width: Math.max(duration * slotWidth - 2, slotWidth - 2),
                  }
                })

                const showPreview = dragUI.active && dragUI.printerId === printer.id

                return (
                  <div 
                    key={printer.id}
                    className={clsx("flex border-b border-farm-800 relative", dragUI.active && "bg-print-900/10")} 
                    style={{ height: ROW_HEIGHT }}
                  >
                    <div className="flex-shrink-0 bg-farm-950 border-r border-farm-700 px-3 py-2 flex items-center" style={{ width: PRINTER_COL_WIDTH }}>
                      <div>
                        <div className="font-medium text-sm">{printer.name}</div>
                        <div className="text-xs text-farm-500">{printer.loaded_colors?.slice(0, 3).join(', ')}</div>
                      </div>
                    </div>
                    
                    <div className="flex-1 relative bg-farm-900/50 timeline-grid" data-printer-id={printer.id}>
                      <div className="absolute inset-0 flex pointer-events-none">
                        {Array.from({ length: totalSlots }).map((_, i) => (
                          <div
                            key={i}
                            className={clsx('flex-shrink-0 border-r border-farm-800/50', i % 96 === 0 && 'border-l border-l-farm-700')}
                            style={{ width: slotWidth }}
                          />
                        ))}
                      </div>
                      
                      {showPreview && (
                        <div
                          className="absolute top-1 bottom-1 rounded-md bg-print-500/50 border-2 border-print-400 z-30 flex items-center justify-center"
                          style={{ left: dragUI.left, width: dragUI.width }}
                        >
                          <span className="text-xs font-bold text-white bg-print-600 px-2 py-1 rounded">{dragUI.label}</span>
                        </div>
                      )}
                      
                      {jobBlocks.map((block) => {
                        const canDrag = block.status === 'scheduled' || block.status === 'pending'
                        const isDragging = dragUI.active && dragUI.jobId === block.job_id
                        return (
                          <div
                            key={block.job_id}
                            onMouseDown={canDrag ? (e) => handleJobMouseDown(e, block) : undefined}
                            className={clsx(
                              'absolute top-1 bottom-1 rounded-md px-2 py-1 overflow-hidden select-none',
                              block.is_setup ? 'bg-amber-700/50' : statusColors[block.status],
                              canDrag && 'cursor-grab',
                              isDragging && 'opacity-30'
                            )}
                            style={{ left: block.left, width: block.width, zIndex: isDragging ? 5 : 10 }}
                          >
                            <div className="flex items-center gap-1">
                              {canDrag && <GripVertical size={12} className="text-white/50" />}
                              <span className="text-xs font-medium truncate text-white/90">
                                {block.is_setup ? 'Setup' : block.item_name}
                              </span>
                            </div>
                          </div>
                        )
                      })}
                    </div>
                  </div>
                )
              })}
            </div>
          </div>
        )}
      </div>

      <div className="p-4 border-t border-farm-800 bg-farm-950 flex items-center gap-6">
        <span className="text-sm text-farm-500">Status:</span>
        {Object.entries(statusColors).map(([status, color]) => (
          <div key={status} className="flex items-center gap-2">
            <div className={clsx('w-3 h-3 rounded', color)} />
            <span className="text-sm text-farm-400 capitalize">{status}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
