import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { RowCard, RowCardList } from './RowCard'

afterEach(cleanup)

describe('RowCard', () => {
  it('renders title, status, fields and actions; skips empty fields', () => {
    render(
      <RowCardList label="Things">
        <RowCard title="Alpha" status={<span>Done</span>} fields={[['Printer', 'P1'], ['User', null], ['Note', ''], ['Zero', 0]]} actions={<button>Go</button>} />
      </RowCardList>,
    )
    expect(screen.getByRole('list', { name: 'Things' })).toBeInTheDocument()
    expect(screen.getByText('Alpha')).toBeInTheDocument()
    expect(screen.getByText('Done')).toBeInTheDocument()
    expect(screen.getByText('Printer')).toBeInTheDocument()
    expect(screen.queryByText('User')).not.toBeInTheDocument()
    expect(screen.queryByText('Note')).not.toBeInTheDocument()
    expect(screen.getByText('Zero')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Go' })).toBeInTheDocument()
  })

  it('makes the title a button when onOpen is given', () => {
    const open = vi.fn()
    render(<RowCardList label="x"><RowCard title="Open me" onOpen={open} /></RowCardList>)
    fireEvent.click(screen.getByRole('button', { name: 'Open me' }))
    expect(open).toHaveBeenCalledOnce()
  })
})
