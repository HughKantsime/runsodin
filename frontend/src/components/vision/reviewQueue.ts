/**
 * Pure queue logic for the Vigil "Review pending" mode. No React, no I/O.
 *
 * The queue holds pending detections in session order. Labelling an item moves
 * the cursor to the next item that has no label yet. Skipping and going back
 * only move the cursor. Items stay in `items`, so "back" can return to
 * anything already seen in this session.
 *
 * Paging: labelled items leave the server's pending set, skipped and unlabelled
 * ones stay in it. The next page offset is therefore (unlabelled items held
 * locally) + (duplicates seen so far, which come from newly arrived detections
 * pushing the set down).
 */

export type ReviewLabel = 'confirmed' | 'dismissed'

export interface QueueItem { id: number; status: string }

export interface ReviewQueue<T extends QueueItem> {
  items: T[]
  index: number
  labels: Record<number, ReviewLabel>
  reviewed: number
  /** Pending count reported by the server at the last fetch. */
  serverTotal: number
  /** `reviewed` at the time of that fetch. */
  reviewedAtFetch: number
  /** Duplicate rows returned by later pages (new arrivals shifted the set). */
  dupes: number
  /** The server returned an empty page: nothing more to load. */
  exhausted: boolean
}

export interface QueueProgress { reviewed: number; total: number; left: number }

function pendingOnly<T extends QueueItem>(items: T[]): T[] {
  const seen = new Set<number>()
  const out: T[] = []
  for (const item of items || []) {
    if (item.status !== 'pending' || seen.has(item.id)) continue
    seen.add(item.id)
    out.push(item)
  }
  return out
}

export function createQueue<T extends QueueItem>(items: T[], total: number): ReviewQueue<T> {
  const list = pendingOnly(items)
  return {
    items: list,
    index: 0,
    labels: {},
    reviewed: 0,
    serverTotal: Math.max(total || 0, 0),
    reviewedAtFetch: 0,
    dupes: 0,
    exhausted: (items || []).length === 0,
  }
}

export function appendItems<T extends QueueItem>(queue: ReviewQueue<T>, items: T[], total: number): ReviewQueue<T> {
  const known = new Set(queue.items.map(i => i.id))
  const pending = pendingOnly(items)
  const fresh = pending.filter(i => !known.has(i.id))
  return {
    ...queue,
    items: [...queue.items, ...fresh],
    serverTotal: Math.max(total || 0, 0),
    reviewedAtFetch: queue.reviewed,
    dupes: queue.dupes + (pending.length - fresh.length),
    exhausted: (items || []).length === 0,
  }
}

export function currentItem<T extends QueueItem>(queue: ReviewQueue<T>): T | undefined {
  return queue.items[queue.index]
}

export function labelOf<T extends QueueItem>(queue: ReviewQueue<T>, id: number): ReviewLabel | undefined {
  return queue.labels[id]
}

export function hasPrevious<T extends QueueItem>(queue: ReviewQueue<T>): boolean {
  return queue.index > 0
}

export function skip<T extends QueueItem>(queue: ReviewQueue<T>): ReviewQueue<T> {
  if (queue.index >= queue.items.length) return queue
  return { ...queue, index: queue.index + 1 }
}

export function back<T extends QueueItem>(queue: ReviewQueue<T>): ReviewQueue<T> {
  if (queue.index <= 0) return queue
  return { ...queue, index: queue.index - 1 }
}

/**
 * Record a successful label for `id` and advance to the next unlabelled item
 * after it (or past the end when there is none yet). Relabelling an item that
 * is already labelled changes its label but does not count again.
 */
export function labelItem<T extends QueueItem>(queue: ReviewQueue<T>, id: number, label: ReviewLabel): ReviewQueue<T> {
  const pos = queue.items.findIndex(i => i.id === id)
  if (pos < 0) return queue
  const labels = { ...queue.labels, [id]: label }
  const reviewed = queue.labels[id] ? queue.reviewed : queue.reviewed + 1
  let next = pos + 1
  while (next < queue.items.length && labels[queue.items[next].id]) next++
  return { ...queue, labels, reviewed, index: next }
}

export function unlabelledCount<T extends QueueItem>(queue: ReviewQueue<T>): number {
  return queue.items.filter(i => !queue.labels[i.id]).length
}

export function progress<T extends QueueItem>(queue: ReviewQueue<T>): QueueProgress {
  const total = queue.serverTotal + queue.reviewedAtFetch
  return { reviewed: queue.reviewed, total, left: Math.max(0, total - queue.reviewed) }
}

export function nextOffset<T extends QueueItem>(queue: ReviewQueue<T> | null): number {
  return queue ? unlabelledCount(queue) + queue.dupes : 0
}

/** True when the cursor is past the local items and the server still has more. */
export function needsMore<T extends QueueItem>(queue: ReviewQueue<T>): boolean {
  if (queue.index < queue.items.length || queue.exhausted) return false
  const { left } = progress(queue)
  return left - unlabelledCount(queue) > 0
}

export function isDone<T extends QueueItem>(queue: ReviewQueue<T>): boolean {
  return queue.index >= queue.items.length && !needsMore(queue)
}

export function progressText(p: QueueProgress): string {
  return `${p.reviewed} of ${p.total} reviewed this session, ${p.left} left`
}
