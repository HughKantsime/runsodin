import { describe, expect, it } from 'vitest'
import {
  appendItems, back, createQueue, currentItem, hasPrevious, isDone, labelItem, labelOf,
  needsMore, nextOffset, progress, progressText, skip,
} from './reviewQueue'

const d = (id: number, status = 'pending') => ({ id, status })
const ids = (q: ReturnType<typeof createQueue>) => q.items.map(i => i.id)

describe('reviewQueue', () => {
  it('starts on the first pending item and drops non-pending and duplicate rows', () => {
    const q = createQueue([d(1), d(2, 'confirmed'), d(3), d(3)], 3)
    expect(ids(q)).toEqual([1, 3])
    expect(currentItem(q)?.id).toBe(1)
    expect(hasPrevious(q)).toBe(false)
  })

  it('labelling advances to the next unlabelled item and counts once', () => {
    let q = createQueue([d(1), d(2), d(3)], 3)
    q = labelItem(q, 1, 'confirmed')
    expect(currentItem(q)?.id).toBe(2)
    expect(labelOf(q, 1)).toBe('confirmed')
    expect(progress(q)).toEqual({ reviewed: 1, total: 3, left: 2 })
  })

  it('skip moves on without labelling and back returns', () => {
    let q = createQueue([d(1), d(2)], 2)
    q = skip(q)
    expect(currentItem(q)?.id).toBe(2)
    expect(progress(q).reviewed).toBe(0)
    q = back(q)
    expect(currentItem(q)?.id).toBe(1)
    expect(back(q)).toBe(q)
  })

  it('relabelling a previous item does not count twice and skips already labelled ones', () => {
    let q = createQueue([d(1), d(2), d(3)], 3)
    q = labelItem(q, 1, 'confirmed')
    q = labelItem(q, 2, 'dismissed')
    q = back(q)
    q = back(q)
    expect(currentItem(q)?.id).toBe(1)
    q = labelItem(q, 1, 'dismissed')
    expect(labelOf(q, 1)).toBe('dismissed')
    expect(progress(q).reviewed).toBe(2)
    expect(currentItem(q)?.id).toBe(3)
  })

  it('ignores a label for an id that is not in the queue', () => {
    const q = createQueue([d(1)], 1)
    expect(labelItem(q, 99, 'confirmed')).toBe(q)
  })

  it('reports session progress across pages: 12 of 216, 204 left', () => {
    let q = createQueue(Array.from({ length: 30 }, (_, i) => d(i + 1)), 216)
    for (let i = 1; i <= 12; i++) q = labelItem(q, i, i % 2 ? 'confirmed' : 'dismissed')
    expect(progressText(progress(q))).toBe('12 of 216 reviewed this session, 204 left')
    // later fetch: server now reports 204 pending
    q = appendItems(q, [d(31)], 204)
    expect(progress(q)).toEqual({ reviewed: 12, total: 216, left: 204 })
  })

  it('asks for more only when the cursor is past the end and the server has more', () => {
    let q = createQueue([d(1), d(2)], 5)
    expect(needsMore(q)).toBe(false)
    q = labelItem(q, 1, 'confirmed')
    q = skip(q)
    expect(needsMore(q)).toBe(true)
    // one skipped item stays pending on the server: offset = 1 unlabelled
    expect(nextOffset(q)).toBe(1)
  })

  it('does not ask for more when everything is accounted for', () => {
    let q = createQueue([d(1), d(2)], 2)
    q = labelItem(q, 1, 'confirmed')
    q = labelItem(q, 2, 'dismissed')
    expect(needsMore(q)).toBe(false)
    expect(isDone(q)).toBe(true)
  })

  it('appended items continue at the cursor and duplicates push the offset forward', () => {
    let q = createQueue([d(1), d(2)], 4)
    q = skip(skip(q))
    q = appendItems(q, [d(2), d(3), d(4)], 4)
    expect(ids(q)).toEqual([1, 2, 3, 4])
    expect(currentItem(q)?.id).toBe(3)
    expect(q.dupes).toBe(1)
    expect(nextOffset(q)).toBe(4 + 1)
  })

  it('an empty page marks the queue exhausted so it never loops', () => {
    let q = createQueue([d(1)], 9)
    q = skip(q)
    expect(needsMore(q)).toBe(true)
    q = appendItems(q, [], 9)
    expect(q.exhausted).toBe(true)
    expect(needsMore(q)).toBe(false)
    expect(isDone(q)).toBe(true)
  })

  it('an empty first page is immediately done', () => {
    const q = createQueue([], 0)
    expect(isDone(q)).toBe(true)
    expect(currentItem(q)).toBeUndefined()
  })
})
