/**
 * HelpTooltip — inline contextual help for complex features.
 *
 * Usage:
 *   <HelpTooltip text="Color-match scoring compares the filament color loaded in each AMS slot against the colors required by the .3mf file." />
 *
 * Renders as a small (?) icon that shows a tooltip on hover/tap.
 * Links to docs when a docsPath is provided.
 */

import { useState, useRef, useEffect } from 'react'
import { HelpCircle, ExternalLink } from 'lucide-react'

interface HelpTooltipProps {
  text: string
  docsPath?: string  // e.g., "/features/color-matching" → docs.runsodin.com/features/color-matching
  position?: 'top' | 'bottom' | 'left' | 'right'
  size?: number
}

export default function HelpTooltip({ text, docsPath, position = 'top', size = 14 }: HelpTooltipProps) {
  const [isOpen, setIsOpen] = useState(false)
  const tooltipRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)

  // Close on outside click
  useEffect(() => {
    if (!isOpen) return
    const handler = (e: MouseEvent) => {
      if (tooltipRef.current && !tooltipRef.current.contains(e.target as Node) &&
          triggerRef.current && !triggerRef.current.contains(e.target as Node)) {
        setIsOpen(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [isOpen])

  const positionClasses = {
    top: 'bottom-full left-1/2 -translate-x-1/2 mb-2',
    bottom: 'top-full left-1/2 -translate-x-1/2 mt-2',
    left: 'right-full top-1/2 -translate-y-1/2 mr-2',
    right: 'left-full top-1/2 -translate-y-1/2 ml-2',
  }

  return (
    <span className="relative inline-flex items-center">
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setIsOpen(!isOpen)}
        onMouseEnter={() => setIsOpen(true)}
        onMouseLeave={() => setIsOpen(false)}
        className="inline-flex items-center justify-center text-muted-foreground hover:text-foreground transition-colors focus:outline-none"
        aria-label="Help"
      >
        <HelpCircle size={size} />
      </button>

      {isOpen && (
        <div
          ref={tooltipRef}
          className={`absolute z-50 ${positionClasses[position]} w-72 max-w-sm`}
          role="tooltip"
        >
          <div className="rounded-lg border border-border bg-popover p-3 shadow-xl text-sm text-popover-foreground leading-relaxed">
            {text}
            {docsPath && (
              <a
                href={`https://docs.runsodin.com${docsPath}`}
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center gap-1 mt-2 text-xs text-accent hover:underline"
              >
                Learn more <ExternalLink size={10} />
              </a>
            )}
          </div>
        </div>
      )}
    </span>
  )
}
