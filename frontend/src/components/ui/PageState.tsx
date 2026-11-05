import { type HTMLAttributes } from 'react'
import { useFoundry } from '../../hooks/themePreferences'

interface PageStateProps extends HTMLAttributes<HTMLDivElement> {
  kind: 'loading' | 'error' | 'empty'
}

/** Presentation only: the caller retains its message, retry action and query state. */
export default function PageState({ kind, ...props }: PageStateProps) {
  const foundry = useFoundry()
  return <div data-foundry-ui={foundry ? kind : undefined}
    role={foundry ? (kind === 'error' ? 'alert' : 'status') : undefined}
    aria-busy={foundry && kind === 'loading' ? true : undefined} {...props} />
}
