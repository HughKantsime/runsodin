import { useMemo, useState } from 'react'
import { useInfiniteQuery } from '@tanstack/react-query'
import { ClipboardCheck, Clock3, RefreshCw } from 'lucide-react'
import { education } from '../../api'
import type {
  EducationCostCenter,
  EducationSubmission,
  EducationSubmissionStatus,
} from '../../types'
import { Button, Card, EmptyState, Select, StatusBadge } from '../ui'
import ReviewDecisionModal from './ReviewDecisionModal'

interface SubmissionQueueProps {
  centers: EducationCostCenter[]
  reviewable: boolean
  mineOnly?: boolean
}

const STATUS_OPTIONS: Array<{ value: string; label: string }> = [
  { value: '', label: 'All statuses' },
  { value: 'submitted', label: 'Awaiting review' },
  { value: 'pending', label: 'Approved' },
  { value: 'scheduled', label: 'Scheduled' },
  { value: 'printing', label: 'Printing' },
  { value: 'completed', label: 'Completed' },
  { value: 'rejected', label: 'Rejected' },
  { value: 'failed', label: 'Failed' },
  { value: 'cancelled', label: 'Cancelled' },
]

const formatDate = (value: string | null) => {
  if (!value) return 'Unknown time'
  const parsed = new Date(value)
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString()
}

export default function SubmissionQueue({ centers, reviewable, mineOnly = false }: SubmissionQueueProps) {
  const [status, setStatus] = useState('')
  const [centerId, setCenterId] = useState('')
  const [reviewTarget, setReviewTarget] = useState<EducationSubmission | null>(null)
  const centerNames = useMemo(
    () => Object.fromEntries(centers.map((center) => [center.id, center.name])),
    [centers],
  )

  const query = useInfiniteQuery({
    queryKey: ['education-submissions', status, centerId, mineOnly],
    queryFn: ({ pageParam }) => education.listSubmissions({
      status: (status || undefined) as EducationSubmissionStatus | undefined,
      costCenterId: centerId ? Number(centerId) : undefined,
      cursor: pageParam,
      limit: 25,
    }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next_cursor || undefined,
  })

  const submissions = query.data?.pages.flatMap((page) => page.items) || []

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-3 sm:flex-row">
        <Select
          aria-label="Filter submissions by status"
          value={status}
          onChange={(event) => setStatus(event.target.value)}
          options={STATUS_OPTIONS}
          wrapperClassName="sm:w-52"
        />
        <Select
          aria-label="Filter submissions by class or club"
          value={centerId}
          onChange={(event) => setCenterId(event.target.value)}
          wrapperClassName="sm:w-64"
        >
          <option value="">All classes and clubs</option>
          {centers.map((center) => (
            <option key={center.id} value={center.id}>{center.name}</option>
          ))}
        </Select>
        <Button
          variant="ghost"
          icon={RefreshCw}
          onClick={() => query.refetch()}
          loading={query.isFetching && !query.isFetchingNextPage}
        >
          Refresh
        </Button>
      </div>

      {query.isLoading ? (
        <Card className="flex min-h-48 items-center justify-center text-sm text-[var(--brand-text-secondary)]">
          <RefreshCw size={16} className="mr-2 animate-spin" /> Loading submissions…
        </Card>
      ) : query.error ? (
        <Card>
          <p role="alert" className="text-sm text-[var(--status-failed)]">{query.error.message}</p>
          <Button className="mt-3" variant="secondary" onClick={() => query.refetch()}>Try again</Button>
        </Card>
      ) : submissions.length === 0 ? (
        <Card>
          <EmptyState
            icon={mineOnly ? Clock3 : ClipboardCheck}
            title={mineOnly ? 'No submissions yet' : 'Review queue is clear'}
            description={mineOnly
              ? 'Your submitted prints and their approval status will appear here.'
              : 'New student submissions will appear here when they need attention.'}
          />
        </Card>
      ) : (
        <div className="grid gap-3">
          {submissions.map((submission) => {
            const canReview = reviewable && submission.status === 'submitted'
            return (
              <Card key={submission.id} padding="sm" className="border border-[var(--brand-card-border)]">
                <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <h3 className="truncate text-sm font-semibold text-[var(--brand-text-primary)]">
                        {submission.item_name}
                      </h3>
                      <StatusBadge status={submission.status} variant="badge" />
                    </div>
                    <p className="mt-1 text-xs text-[var(--brand-text-secondary)]">
                      {centerNames[submission.cost_center_id] || `Cost center ${submission.cost_center_id}`}
                      {!mineOnly && ` · ${submission.submitter_username}`}
                      {' · '}{formatDate(submission.created_at)}
                    </p>
                    {submission.rejection_reason && (
                      <p className="mt-2 text-xs text-[var(--status-failed)]">
                        {submission.rejection_reason}
                      </p>
                    )}
                  </div>
                  {canReview && (
                    <Button
                      variant="secondary"
                      size="sm"
                      icon={ClipboardCheck}
                      className="w-full flex-shrink-0 sm:w-auto"
                      onClick={() => setReviewTarget(submission)}
                    >
                      Review
                    </Button>
                  )}
                </div>
              </Card>
            )
          })}
        </div>
      )}

      {query.hasNextPage && (
        <div className="flex justify-center">
          <Button
            variant="secondary"
            onClick={() => query.fetchNextPage()}
            loading={query.isFetchingNextPage}
          >
            Load more
          </Button>
        </div>
      )}

      <ReviewDecisionModal submission={reviewTarget} onClose={() => setReviewTarget(null)} />
    </div>
  )
}
