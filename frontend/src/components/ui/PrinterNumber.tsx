import { createElement, type HTMLAttributes } from 'react'
import { useFoundry } from '../../hooks/themePreferences'

/** Typography only. The caller supplies the reported value, units and unknown state. */
export default function PrinterNumber({ as = 'span', ...props }: HTMLAttributes<HTMLElement> & { as?: 'span' | 'p' | 'td' | 'dd' }) {
  const foundry = useFoundry()
  return createElement(as, { ...props, 'data-foundry-number': foundry ? '' : undefined })
}
