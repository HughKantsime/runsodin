import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { expect, it, type Mock } from 'vitest'

type Page = {
  item: 'printer' | 'job' | 'spool'
  mount: (affected: number, skips: { id: number; detail: string }[]) => Promise<void>
  checkbox: (name: string) => HTMLElement
  action: string
  notices: { partial: Mock; error: Mock; success: Mock }
}

// Each page supplies its actual response shape. Names on screen are deliberately different from IDs.
export const bulkNames = ['Birch', 'Cedar', 'Pine']
export function testBulkUpdatePage({ item, mount, checkbox, action, notices }: Page) {
  const reason = `This ${item} belongs to no organization. Ask a platform administrator to change it.`
  const other = 'The item is busy.'
  it.each([
    { scenario: 'all changed', affected: 3, details: [], message: '' },
    { scenario: 'partly changed', affected: 2, details: [reason], message: `1 (Pine): ${reason}` },
    { scenario: 'nothing changed', affected: 0, details: [reason, reason, reason], message: `3 (Birch, Cedar, Pine): ${reason}` },
    { scenario: 'two reasons, one repeated', affected: 0, details: [reason, other, reason], message: `2 (Birch, Pine): ${reason}\n1 (Cedar): ${other}` },
  ])('bulk action: $scenario', async ({ affected, details, message }) => {
    await mount(affected, details.map((detail, i) => ({ id: 41 + affected + i, detail })))
    bulkNames.forEach(name => fireEvent.click(checkbox(name)))
    const toolbar = screen.getByText('3 selected').parentElement!
    expect(toolbar).toHaveClass('flex-wrap')
    fireEvent.click(within(toolbar).getByRole('button', { name: action }))

    if (details.length) {
      const text = `${affected} ${item}${affected === 1 ? '' : 's'} updated. ${details.length} not changed.\n${message}`
      const notice = affected ? notices.partial : notices.error
      await waitFor(() => expect(notice).toHaveBeenCalledExactlyOnceWith(text, expect.objectContaining({
        style: expect.objectContaining({ overflowWrap: 'anywhere', maxWidth: 'min(480px, calc(100vw - 32px))' }),
      })))
    }
    await waitFor(() => bulkNames.forEach((name, i) => expect((checkbox(name) as HTMLInputElement).checked).toBe(i >= affected)))
    expect(notices.success).toHaveBeenCalledTimes(item === 'job' && affected === 3 ? 1 : 0)
    expect(notices.partial).toHaveBeenCalledTimes(affected === 2 ? 1 : 0)
    expect(notices.error).toHaveBeenCalledTimes(affected === 0 ? 1 : 0)
  })
}
