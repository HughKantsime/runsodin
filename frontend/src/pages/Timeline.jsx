import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { 
  ChevronLeft, 
  ChevronRight, 
  ZoomIn, 
  ZoomOut,
  Calendar as CalendarIcon
} from 'lucide-react'
import { format, addDays, startOfDay, differenceInMinutes, isSameDay } from 'date-fns'
import clsx from 'clsx'

import { timeline, printers } from '../api'

const SLOT_WIDTH = 20 // pixels per 30-min slot
const ROW_HEIGHT = 56

const statusColors = {
  pending: 'bg-status-pending',
  scheduled: 'bg-status-scheduled',
  printing: 'bg-status-printing',
  completed: 'bg-status-completed',
  failed: 'bg-status-failed',
}

function TimelineHeader({ startDate, days, slotWidth }) {
  const slots = []
  const totalSlots = days * 96 // 15-min slots per day

  for (let i = 0; i < totalSlots; i++) {
    const slotTime = new Date(startDate.getTime() + i * 30 * 60 * 1000)
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
        style={{ width: 120 }}
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
        style={{ width: slotWidth * 48 }}
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
        style={{ width: 120 }}
      />
      <div className="flex">
        {dayHeaders}
      </div>
    </div>
  )
}

function TimelineRow({ printer, slots, startDate, days, slotWidth }) {
  const totalSlots = days * 96
  
  // Calculate job positions
  const jobBlocks = slots.map((slot) => {
    const startMinutes = differenceInMinutes(new Date(slot.start), startDate)
    const endMinutes = differenceInMinutes(new Date(slot.end), startDate)
    const startSlot = Math.floor(startMinutes / 30)
    const duration = Math.ceil((endMinutes - startMinutes) / 30)
    
    return {
      ...slot,
      left: startSlot * slotWidth,
      width: Math.max(duration * slotWidth - 2, slotWidth - 2),
    }
  })

  return (
    <div className="flex border-b border-farm-800 relative" style={{ height: ROW_HEIGHT }}>
      {/* Printer name */}
      <div 
        className="flex-shrink-0 bg-farm-950 border-r border-farm-700 px-3 py-2 flex items-center"
        style={{ width: 120 }}
      >
        <div>
          <div className="font-medium text-sm">{printer.name}</div>
          <div className="text-xs text-farm-500">
            {printer.loaded_colors?.slice(0, 3).join(', ')}
          </div>
        </div>
      </div>
      
      {/* Timeline slots background */}
      <div className="flex-1 relative bg-farm-900/50">
        {/* Grid lines */}
        <div className="absolute inset-0 flex">
          {Array.from({ length: totalSlots }).map((_, i) => (
            <div
              key={i}
              className={clsx(
                'flex-shrink-0 border-r border-farm-800/50',
                i % 48 === 0 && 'border-l border-l-farm-700'
              )}
              style={{ width: slotWidth }}
            />
          ))}
        </div>
        
        {/* Job blocks */}
        {jobBlocks.map((block) => (
          <div
            key={block.job_id}
            className={clsx(
              'absolute top-1 bottom-1 rounded-md px-2 py-1 overflow-hidden cursor-pointer',
              'transition-all hover:brightness-110 hover:z-10',
              block.is_setup ? 'bg-amber-700/50' : statusColors[block.status]
            )}
            style={{
              left: block.left,
              width: block.width,
            }}
            title={`${block.item_name}\n${format(new Date(block.start), 'MMM d HH:mm')} - ${format(new Date(block.end), 'HH:mm')}`}
          >
            <div className="text-xs font-medium truncate text-white/90">
              {block.is_setup ? 'Setup' : block.item_name}
            </div>
            {block.width > 80 && (
              <div className="text-[10px] text-white/60 truncate">
                {format(new Date(block.start), 'HH:mm')}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}

function NowIndicator({ startDate, slotWidth }) {
  const now = new Date()
  const minutesFromStart = differenceInMinutes(now, startDate)
  
  if (minutesFromStart < 0) return null
  
  const left = 120 + (minutesFromStart / 30) * slotWidth

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
  const [startDate, setStartDate] = useState(() => startOfDay(new Date()))
  const [days, setDays] = useState(7)
  const [slotWidth, setSlotWidth] = useState(SLOT_WIDTH)

  const { data: printersData } = useQuery({
    queryKey: ['printers'],
    queryFn: () => printers.list(true),
  })

  const { data: timelineData, isLoading } = useQuery({
    queryKey: ['timeline', startDate.toISOString(), days],
    queryFn: () => timeline.get(startDate.toISOString(), days),
  })

  const goToPrevWeek = () => setStartDate(d => addDays(d, -7))
  const goToNextWeek = () => setStartDate(d => addDays(d, 7))
  const goToToday = () => setStartDate(startOfDay(new Date()))

  // Group slots by printer
  const slotsByPrinter = {}
  timelineData?.slots?.forEach((slot) => {
    if (!slotsByPrinter[slot.printer_id]) {
      slotsByPrinter[slot.printer_id] = []
    }
    slotsByPrinter[slot.printer_id].push(slot)
  })

  return (
    <div className="h-full flex flex-col">
      {/* Toolbar */}
      <div className="flex items-center justify-between p-4 border-b border-farm-800 bg-farm-950">
        <div className="flex items-center gap-2">
          <button
            onClick={goToPrevWeek}
            className="p-2 bg-farm-800 hover:bg-farm-700 rounded-lg transition-colors"
          >
            <ChevronLeft size={20} />
          </button>
          <button
            onClick={goToToday}
            className="px-4 py-2 bg-farm-800 hover:bg-farm-700 rounded-lg transition-colors flex items-center gap-2"
          >
            <CalendarIcon size={16} />
            Today
          </button>
          <button
            onClick={goToNextWeek}
            className="p-2 bg-farm-800 hover:bg-farm-700 rounded-lg transition-colors"
          >
            <ChevronRight size={20} />
          </button>
          
          <span className="ml-4 text-farm-400">
            {format(startDate, 'MMM d')} — {format(addDays(startDate, days - 1), 'MMM d, yyyy')}
          </span>
        </div>

        <div className="flex items-center gap-2">
          <button
            onClick={() => setSlotWidth(w => Math.max(20, w - 10))}
            className="p-2 bg-farm-800 hover:bg-farm-700 rounded-lg transition-colors"
            title="Zoom out"
          >
            <ZoomOut size={18} />
          </button>
          <button
            onClick={() => setSlotWidth(w => Math.min(80, w + 10))}
            className="p-2 bg-farm-800 hover:bg-farm-700 rounded-lg transition-colors"
            title="Zoom in"
          >
            <ZoomIn size={18} />
          </button>
          
          <select
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
            className="bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm"
          >
            <option value={3}>3 days</option>
            <option value={7}>1 week</option>
            <option value={14}>2 weeks</option>
          </select>
        </div>
      </div>

      {/* Timeline */}
      <div className="flex-1 overflow-auto relative">
        {isLoading ? (
          <div className="flex items-center justify-center h-64">
            <div className="text-farm-500">Loading timeline...</div>
          </div>
        ) : (
          <div className="min-w-max">
            {/* Date header */}
            <TimelineDateHeader 
              startDate={startDate} 
              days={days} 
              slotWidth={slotWidth} 
            />
            
            {/* Time header */}
            <TimelineHeader 
              startDate={startDate} 
              days={days} 
              slotWidth={slotWidth} 
            />
            
            {/* Printer rows */}
            <div className="relative">
              <NowIndicator startDate={startDate} slotWidth={slotWidth} />
              
              {printersData?.map((printer) => (
                <TimelineRow
                  key={printer.id}
                  printer={printer}
                  slots={slotsByPrinter[printer.id] || []}
                  startDate={startDate}
                  days={days}
                  slotWidth={slotWidth}
                />
              ))}
              
              {(!printersData || printersData.length === 0) && (
                <div className="p-8 text-center text-farm-500">
                  No printers configured. Add printers to see the timeline.
                </div>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Legend */}
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
