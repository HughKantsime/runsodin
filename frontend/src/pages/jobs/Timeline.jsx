import { useState, useRef, useEffect, useCallback } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  ChevronLeft,
  ChevronRight,
  ZoomIn,
  ZoomOut,
  Calendar as CalendarIcon,
  GripVertical,
  Clock,
  Star,
  Layers,
  Thermometer,
  AlertTriangle,
  CalendarX2,
} from 'lucide-react'
import { format, addDays, startOfDay, differenceInMinutes, isSameDay } from 'date-fns'
import { useNavigate } from 'react-router-dom'
import clsx from 'clsx'
import toast from 'react-hot-toast'

import { timeline, printers, jobActions, jobs, printJobs } from '../../api'
import DetailDrawer from '../../components/shared/DetailDrawer'

function parseUTC(dateStr) {
  if (!dateStr) return null
  if (!dateStr.endsWith('Z') && !dateStr.includes('+')) {
    dateStr = dateStr + 'Z'
  }
  return new Date(dateStr)
}

const SLOT_MINUTES = 15
const ROW_HEIGHT = 56
const PRINTER_COL_WIDTH = 120
const ZOOM_MIN = 10
const ZOOM_MAX = 60

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
        className="flex-shrink-0 bg-farm-900 border-r border-farm-700 px-3 py-2 font-medium text-sm"
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
          'flex-shrink-0 text-center py-1 border-r border-farm-700 text-sm',
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
  const navigate = useNavigate()
  const containerRef = useRef(null)
  const scrollAreaRef = useRef(null)
  const [startDate, setStartDate] = useState(() => startOfDay(new Date()))
  const [days, setDays] = useState(7)
  const [slotWidth, setSlotWidth] = useState(20)

  const dragRef = useRef({
    active: false,
    job: null,
    printerId: null,
    slotIndex: null,
    newStartTime: null
  })

  const [dragUI, setDragUI] = useState({ active: false, printerId: null, left: 0, width: 0, label: '', jobId: null })
  const [selectedBlock, setSelectedBlock] = useState(null)

  const { data: printersData } = useQuery({
    queryKey: ['printers'],
    queryFn: () => printers.list(true),
  })

  const { data: timelineData, isLoading } = useQuery({
    queryKey: ['timeline', startDate.toISOString(), days],
    queryFn: () => timeline.get(startDate.toISOString(), days),
    refetchInterval: 30000,
  })

  // Auto-scroll to Now indicator on mount
  useEffect(() => {
    if (!scrollAreaRef.current || !timelineData) return
    const now = new Date()
    const minutesFromStart = differenceInMinutes(now, startDate)
    if (minutesFromStart < 0) return
    const nowLeft = (minutesFromStart / SLOT_MINUTES) * slotWidth
    const containerWidth = scrollAreaRef.current.clientWidth
    scrollAreaRef.current.scrollLeft = Math.max(0, nowLeft - containerWidth / 2)
  }, [timelineData, startDate, slotWidth])

  const moveJobMutation = useMutation({
    mutationFn: ({ jobId, printerId, scheduledStart }) =>
      jobActions.move(jobId, printerId, scheduledStart),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: ['timeline'] })
      queryClient.invalidateQueries({ queryKey: ['jobs'] })
      const newTime = new Date(variables.scheduledStart)
      toast.success(`Moved job to ${format(newTime, 'MMM d HH:mm')}`)
    },
    onError: (error) => {
      toast.error('Move failed: ' + (error?.message || 'Unknown error'))
    }
  })

  const handleDragMove = useCallback((clientX, clientY) => {
    if (!dragRef.current.active || !containerRef.current) return

    const gridElements = containerRef.current.querySelectorAll('.timeline-grid')
    let targetPrinterId = null
    let targetRect = null

    gridElements.forEach((el) => {
      const rect = el.getBoundingClientRect()
      if (clientY >= rect.top && clientY <= rect.bottom) {
        targetPrinterId = parseInt(el.dataset.printerId)
        targetRect = rect
      }
    })

    if (targetPrinterId && targetRect) {
      const x = clientX - targetRect.left
      const currentSlotWidth = slotWidth
      const idx = Math.max(0, Math.floor(x / currentSlotWidth))

      const job = dragRef.current.job
      const durationMinutes = differenceInMinutes(parseUTC(job.end), parseUTC(job.start))
      const durationSlots = Math.ceil(durationMinutes / SLOT_MINUTES)

      const newTime = new Date(startDate.getTime() + idx * SLOT_MINUTES * 60 * 1000)

      dragRef.current.printerId = targetPrinterId
      dragRef.current.slotIndex = idx
      dragRef.current.newStartTime = newTime

      setDragUI({
        active: true,
        printerId: targetPrinterId,
        left: idx * currentSlotWidth,
        width: durationSlots * currentSlotWidth - 2,
        label: format(newTime, 'MMM d HH:mm'),
        jobId: job.job_id
      })
    }
  }, [slotWidth, startDate])

  const handleDragEnd = useCallback((clientX, clientY) => {
    if (!dragRef.current.active) return

    const { job, printerId, newStartTime, startX, startY } = dragRef.current
    const dx = clientX - (startX || 0)
    const dy = clientY - (startY || 0)
    const distance = Math.sqrt(dx * dx + dy * dy)

    if (distance < 5 && job) {
      setSelectedBlock(job)
    } else if (job && printerId && newStartTime) {
      const isoTime = newStartTime.toISOString()
      moveJobMutation.mutate({
        jobId: job.job_id,
        printerId: printerId,
        scheduledStart: isoTime
      })
    }

    dragRef.current = { active: false, job: null, printerId: null, slotIndex: null, newStartTime: null }
    setDragUI({ active: false, printerId: null, left: 0, width: 0, label: '', jobId: null })
  }, [moveJobMutation])

  useEffect(() => {
    const handleMouseMove = (e) => handleDragMove(e.clientX, e.clientY)
    const handleMouseUp = (e) => handleDragEnd(e.clientX, e.clientY)
    const handleTouchMove = (e) => {
      if (!dragRef.current.active) return
      e.preventDefault()
      const touch = e.touches[0]
      handleDragMove(touch.clientX, touch.clientY)
    }
    const handleTouchEnd = (e) => {
      if (!dragRef.current.active) return
      const touch = e.changedTouches[0]
      handleDragEnd(touch.clientX, touch.clientY)
    }

    window.addEventListener('mousemove', handleMouseMove)
    window.addEventListener('mouseup', handleMouseUp)
    window.addEventListener('touchmove', handleTouchMove, { passive: false })
    window.addEventListener('touchend', handleTouchEnd)

    return () => {
      window.removeEventListener('mousemove', handleMouseMove)
      window.removeEventListener('mouseup', handleMouseUp)
      window.removeEventListener('touchmove', handleTouchMove)
      window.removeEventListener('touchend', handleTouchEnd)
    }
  }, [handleDragMove, handleDragEnd])

  const startDrag = (clientX, clientY, block) => {
    dragRef.current = {
      active: true,
      job: block,
      printerId: null,
      slotIndex: null,
      newStartTime: null,
      startX: clientX,
      startY: clientY
    }
    setDragUI(prev => ({ ...prev, active: true, jobId: block.job_id }))
  }

  const handleJobMouseDown = (e, block) => {
    e.preventDefault()
    startDrag(e.clientX, e.clientY, block)
  }

  const handleJobTouchStart = (e, block) => {
    const touch = e.touches[0]
    startDrag(touch.clientX, touch.clientY, block)
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
      {/* Toolbar */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2 p-3 md:p-4 border-b border-farm-800 bg-farm-950">
        <div className="flex items-center gap-2">
          <button onClick={goToPrevWeek} className="p-1.5 md:p-2 bg-farm-800 hover:bg-farm-700 rounded-lg">
            <ChevronLeft size={18} />
          </button>
          <button onClick={goToToday} className="px-3 py-1.5 md:px-4 md:py-2 bg-farm-800 hover:bg-farm-700 rounded-lg flex items-center gap-2 text-sm">
            <CalendarIcon size={14} />
            Today
          </button>
          <button onClick={goToNextWeek} className="p-1.5 md:p-2 bg-farm-800 hover:bg-farm-700 rounded-lg">
            <ChevronRight size={18} />
          </button>
          <span className="ml-2 text-farm-400 text-xs md:text-sm sm:hidden">
            {format(startDate, 'M/d')} — {format(addDays(startDate, days - 1), 'M/d')}
          </span>
          <span className="ml-2 text-farm-400 text-xs md:text-sm hidden sm:inline">
            {format(startDate, 'MMM d')} — {format(addDays(startDate, days - 1), 'MMM d, yyyy')}
          </span>
        </div>

        <div className="flex items-center gap-2">
          {dragUI.active && <span className="text-xs text-print-400 mr-2">{dragUI.label}</span>}
          <button
            onClick={() => setSlotWidth(w => Math.max(ZOOM_MIN, w - 5))}
            disabled={slotWidth <= ZOOM_MIN}
            className={clsx("p-1.5 md:p-2 rounded-lg", slotWidth <= ZOOM_MIN ? "bg-farm-900 text-farm-700 cursor-not-allowed" : "bg-farm-800 hover:bg-farm-700")}
          >
            <ZoomOut size={16} />
          </button>
          <span className="text-xs text-farm-500 tabular-nums w-6 text-center">{Math.round(((slotWidth - ZOOM_MIN) / (ZOOM_MAX - ZOOM_MIN)) * 100)}%</span>
          <button
            onClick={() => setSlotWidth(w => Math.min(ZOOM_MAX, w + 5))}
            disabled={slotWidth >= ZOOM_MAX}
            className={clsx("p-1.5 md:p-2 rounded-lg", slotWidth >= ZOOM_MAX ? "bg-farm-900 text-farm-700 cursor-not-allowed" : "bg-farm-800 hover:bg-farm-700")}
          >
            <ZoomIn size={16} />
          </button>
          <select value={days} onChange={(e) => setDays(Number(e.target.value))} className="bg-farm-800 border border-farm-700 rounded-lg px-2 py-1.5 text-sm">
            <option value={3}>3 days</option>
            <option value={7}>1 week</option>
            <option value={14}>2 weeks</option>
          </select>
        </div>
      </div>

      <div className="flex-1 overflow-auto relative" ref={scrollAreaRef}>
        {isLoading ? (
          <div className="flex items-center justify-center h-64 text-farm-500 text-sm">Loading...</div>
        ) : (!timelineData?.slots?.length && !printersData?.length) ? (
          <div className="flex flex-col items-center justify-center h-64 text-farm-500 gap-3">
            <CalendarX2 size={32} className="text-farm-600" />
            <p className="text-sm">No scheduled jobs</p>
            <button onClick={() => navigate('/jobs')} className="text-sm text-print-400 hover:text-print-300 transition-colors">Go to Jobs →</button>
          </div>
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
                      <div className="font-medium text-sm truncate">{printer.nickname || printer.name}</div>
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
                          <span className="text-xs font-bold text-white bg-print-600 px-2 py-1 rounded-lg">{dragUI.label}</span>
                        </div>
                      )}
                      
                      {jobBlocks.map((block) => {
                        const canDrag = block.status === 'scheduled' || block.status === 'pending'
                        const isDragging = dragUI.active && dragUI.jobId === block.job_id
                        return (
                          <div
                            key={block.job_id || block.mqtt_job_id || `${block.printer_id}-${block.start}`}
                            onMouseDown={canDrag ? (e) => handleJobMouseDown(e, block) : undefined}
                            onTouchStart={canDrag ? (e) => handleJobTouchStart(e, block) : undefined}
                            onClick={!canDrag && !block.is_setup ? () => setSelectedBlock(block) : undefined}
                            className={clsx(
                              'absolute top-1 bottom-1 rounded-md px-2 py-1 overflow-hidden select-none',
                              block.is_setup ? 'bg-amber-700/50' : statusColors[block.status],
                              canDrag ? 'cursor-grab' : !block.is_setup ? 'cursor-pointer' : '',
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

      {/* Status legend */}
      <div className="p-2 md:p-4 border-t border-farm-800 bg-farm-950 flex items-center gap-3 md:gap-6 flex-wrap">
        <span className="text-xs md:text-sm text-farm-500">Status:</span>
        {Object.entries(statusColors).map(([status, color]) => (
          <div key={status} className="flex items-center gap-1.5">
            <div className={clsx('w-2.5 h-2.5 md:w-3 md:h-3 rounded-lg', color)} />
            <span className="text-xs md:text-sm text-farm-400 capitalize">{status}</span>
          </div>
        ))}
      </div>

      {/* Detail Drawer */}
      <DetailDrawer
        open={!!selectedBlock}
        onClose={() => setSelectedBlock(null)}
        title={selectedBlock?.item_name || selectedBlock?.printer_name || 'Job Details'}
      >
        {selectedBlock && <BlockDetail block={selectedBlock} />}
      </DetailDrawer>
    </div>
  )
}

function BlockDetail({ block }) {
  if (block.job_id) return <ScheduledJobDetail block={block} />
  if (block.mqtt_job_id) return <MqttJobDetail block={block} />
  return <p className="text-farm-500 text-sm">No details available.</p>
}

function ScheduledJobDetail({ block }) {
  const { data: job, isLoading } = useQuery({
    queryKey: ['job', block.job_id],
    queryFn: () => jobs.get(block.job_id),
    enabled: !!block.job_id,
  })

  if (isLoading) return <p className="text-farm-500 text-sm">Loading...</p>
  if (!job) return <p className="text-farm-500 text-sm">Job not found.</p>

  const durationMin = differenceInMinutes(parseUTC(block.end), parseUTC(block.start))
  const hours = Math.floor(durationMin / 60)
  const mins = durationMin % 60

  return (
    <div className="space-y-3">
      {/* Status + name */}
      <div className="bg-farm-900 rounded-lg border border-farm-800 p-4">
        <div className="flex items-center justify-between mb-2">
          <span className="text-farm-100 font-medium">{job.name || block.item_name}</span>
          <span className={clsx('px-2 py-0.5 rounded text-xs font-medium capitalize', statusColors[job.status] || 'bg-farm-700')}>{job.status}</span>
        </div>
        <p className="text-farm-400 text-sm">Printer: {block.printer_name}</p>
      </div>

      {/* Color swatches */}
      {block.colors?.length > 0 && (
        <div className="bg-farm-900 rounded-lg border border-farm-800 p-4">
          <h4 className="text-farm-400 text-xs uppercase tracking-wide mb-2">Colors</h4>
          <div className="flex gap-2 flex-wrap">
            {block.colors.map((c, i) => (
              <div key={i} className="w-6 h-6 rounded-full border border-farm-700" style={{ backgroundColor: c }} title={c} />
            ))}
          </div>
        </div>
      )}

      {/* Schedule & Duration */}
      <div className="bg-farm-900 rounded-lg border border-farm-800 p-4">
        <h4 className="text-farm-400 text-xs uppercase tracking-wide mb-2">Schedule</h4>
        <div className="space-y-1.5 text-sm">
          <Row icon={<Clock size={14} />} label="Start" value={format(parseUTC(block.start), 'MMM d, HH:mm')} />
          <Row icon={<Clock size={14} />} label="End" value={format(parseUTC(block.end), 'MMM d, HH:mm')} />
          <Row label="Duration" value={hours > 0 ? `${hours}h ${mins}m` : `${mins}m`} />
        </div>
      </div>

      {/* Job details */}
      <div className="bg-farm-900 rounded-lg border border-farm-800 p-4">
        <h4 className="text-farm-400 text-xs uppercase tracking-wide mb-2">Details</h4>
        <div className="space-y-1.5 text-sm">
          {job.priority != null && (
            <Row icon={<Star size={14} />} label="Priority" value={job.priority} />
          )}
          {job.filament_type && (
            <Row icon={<Layers size={14} />} label="Filament" value={job.filament_type} />
          )}
          {job.due_date && (
            <Row label="Due" value={format(parseUTC(job.due_date), 'MMM d, yyyy')} />
          )}
          {job.match_score != null && (
            <Row label="Match score" value={`${Math.round(job.match_score * 100)}%`} />
          )}
        </div>
      </div>

      {/* Cost / Price */}
      {(job.estimated_cost != null || job.suggested_price != null) && (
        <div className="bg-farm-900 rounded-lg border border-farm-800 p-4">
          <h4 className="text-farm-400 text-xs uppercase tracking-wide mb-2">Pricing</h4>
          <div className="space-y-1.5 text-sm">
            {job.estimated_cost != null && <Row label="Est. cost" value={`$${Number(job.estimated_cost).toFixed(2)}`} />}
            {job.suggested_price != null && <Row label="Sugg. price" value={`$${Number(job.suggested_price).toFixed(2)}`} />}
          </div>
        </div>
      )}

      {/* Failure info */}
      {job.status === 'failed' && (job.fail_reason || job.fail_notes) && (
        <div className="bg-red-900/30 rounded-lg border border-red-800/50 p-4">
          <h4 className="text-red-400 text-xs uppercase tracking-wide mb-2 flex items-center gap-1.5">
            <AlertTriangle size={14} /> Failure
          </h4>
          <div className="space-y-1.5 text-sm">
            {job.fail_reason && <Row label="Reason" value={job.fail_reason} />}
            {job.fail_notes && <p className="text-farm-300 mt-1">{job.fail_notes}</p>}
          </div>
        </div>
      )}

      {/* Notes */}
      {job.notes && (
        <div className="bg-farm-900 rounded-lg border border-farm-800 p-4">
          <h4 className="text-farm-400 text-xs uppercase tracking-wide mb-2">Notes</h4>
          <p className="text-farm-300 text-sm">{job.notes}</p>
        </div>
      )}
    </div>
  )
}

function MqttJobDetail({ block }) {
  const { data: pj, isLoading } = useQuery({
    queryKey: ['printJob', block.mqtt_job_id],
    queryFn: () => printJobs.get(block.mqtt_job_id),
    enabled: !!block.mqtt_job_id,
  })

  if (isLoading) return <p className="text-farm-500 text-sm">Loading...</p>
  if (!pj) return <p className="text-farm-500 text-sm">Print job not found.</p>

  const progress = pj.progress ?? pj.percent_complete ?? 0

  return (
    <div className="space-y-3">
      {/* Name + status */}
      <div className="bg-farm-900 rounded-lg border border-farm-800 p-4">
        <div className="flex items-center justify-between mb-2">
          <span className="text-farm-100 font-medium">{pj.filename || pj.job_name || block.item_name}</span>
          <span className={clsx('px-2 py-0.5 rounded text-xs font-medium capitalize', statusColors[pj.status] || 'bg-farm-700')}>{pj.status}</span>
        </div>
        <p className="text-farm-400 text-sm">Printer: {block.printer_name}</p>
      </div>

      {/* Progress */}
      <div className="bg-farm-900 rounded-lg border border-farm-800 p-4">
        <h4 className="text-farm-400 text-xs uppercase tracking-wide mb-2">Progress</h4>
        <div className="w-full bg-farm-800 rounded-full h-2.5 mb-2">
          <div
            className="bg-print-500 h-2.5 rounded-full transition-all"
            style={{ width: `${Math.min(progress, 100)}%` }}
          />
        </div>
        <span className="text-farm-300 text-sm">{Math.round(progress)}%</span>
      </div>

      {/* Details */}
      <div className="bg-farm-900 rounded-lg border border-farm-800 p-4">
        <h4 className="text-farm-400 text-xs uppercase tracking-wide mb-2">Details</h4>
        <div className="space-y-1.5 text-sm">
          {pj.current_layer != null && pj.total_layers != null && (
            <Row icon={<Layers size={14} />} label="Layer" value={`${pj.current_layer} / ${pj.total_layers}`} />
          )}
          {pj.remaining_time != null && (
            <Row icon={<Clock size={14} />} label="Remaining" value={formatSeconds(pj.remaining_time)} />
          )}
          {pj.total_duration != null && (
            <Row icon={<Clock size={14} />} label="Total duration" value={formatSeconds(pj.total_duration)} />
          )}
        </div>
      </div>

      {/* Temperatures */}
      {(pj.bed_temp_target != null || pj.nozzle_temp_target != null) && (
        <div className="bg-farm-900 rounded-lg border border-farm-800 p-4">
          <h4 className="text-farm-400 text-xs uppercase tracking-wide mb-2">Temperatures</h4>
          <div className="space-y-1.5 text-sm">
            {pj.bed_temp_target != null && (
              <Row icon={<Thermometer size={14} />} label="Bed target" value={`${pj.bed_temp_target}\u00B0C`} />
            )}
            {pj.nozzle_temp_target != null && (
              <Row icon={<Thermometer size={14} />} label="Nozzle target" value={`${pj.nozzle_temp_target}\u00B0C`} />
            )}
          </div>
        </div>
      )}

      {/* Error */}
      {pj.error_code && (
        <div className="bg-red-900/30 rounded-lg border border-red-800/50 p-4">
          <h4 className="text-red-400 text-xs uppercase tracking-wide mb-2 flex items-center gap-1.5">
            <AlertTriangle size={14} /> Error
          </h4>
          <p className="text-farm-300 text-sm">{pj.error_code}</p>
        </div>
      )}
    </div>
  )
}

function Row({ icon, label, value }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-farm-400 flex items-center gap-1.5">
        {icon}{label}
      </span>
      <span className="text-farm-200">{value}</span>
    </div>
  )
}

function formatSeconds(sec) {
  if (sec == null) return '--'
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  if (h > 0) return `${h}h ${m}m`
  return `${m}m`
}
