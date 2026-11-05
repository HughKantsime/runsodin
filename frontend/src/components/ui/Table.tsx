import { forwardRef, type TableHTMLAttributes } from 'react'
import { useFoundry } from '../../hooks/themePreferences'

/** Keeps the caller's native table structure, column scopes and Classic classes. */
const Table = forwardRef<HTMLTableElement, TableHTMLAttributes<HTMLTableElement>>(function Table(props, ref) {
  const foundry = useFoundry()
  return <table ref={ref} data-foundry-ui={foundry ? 'table' : undefined} {...props} />
})
export default Table
