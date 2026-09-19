import { useEffect, useMemo, useState } from 'react'
import { useInfiniteQuery, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  BookOpenCheck,
  BookOpen,
  Building2,
  ClipboardCheck,
  FileUp,
  GraduationCap,
  Printer,
  ShieldCheck,
  UsersRound,
} from 'lucide-react'
import { education, getEducationMode } from '../../api'
import { useLicense } from '../../LicenseContext'
import type { EducationCapabilities, EducationCostCenter } from '../../types'
import CostCenterManager from '../../components/education/CostCenterManager'
import ClassroomManager from '../../components/education/ClassroomManager'
import SubmissionQueue from '../../components/education/SubmissionQueue'
import SubmissionUploadModal from '../../components/education/SubmissionUploadModal'
import ReadinessPanel from '../../components/education/ReadinessPanel'
import { Button, Card, EmptyState, PageHeader, StatCard, TabBar } from '../../components/ui'

type WorkbenchTab = 'overview' | 'submissions' | 'review' | 'centers' | 'classroom'

export default function EducationWorkbench() {
  const queryClient = useQueryClient()
  const license = useLicense()
  const [activeTab, setActiveTab] = useState<WorkbenchTab>('overview')
  const [uploadOpen, setUploadOpen] = useState(false)
  const [includeArchived, setIncludeArchived] = useState(false)

  const modeQuery = useQuery({ queryKey: ['education-mode'], queryFn: getEducationMode })
  const capabilitiesQuery = useQuery({
    queryKey: ['education-capabilities'],
    queryFn: education.capabilities,
  })
  const capabilities = capabilitiesQuery.data

  const centersQuery = useInfiniteQuery({
    queryKey: ['education-centers', includeArchived],
    queryFn: ({ pageParam }) => education.listCenters({
      includeArchived,
      cursor: pageParam,
      limit: 100,
    }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next_cursor || undefined,
    enabled: !!capabilities?.education_enabled,
  })
  useEffect(() => {
    if (centersQuery.hasNextPage && !centersQuery.isFetchingNextPage) {
      centersQuery.fetchNextPage()
    }
  }, [centersQuery.hasNextPage, centersQuery.isFetchingNextPage])

  const centers = centersQuery.data?.pages.flatMap((page) => page.items) || []
  const activeCenters = centers.filter((center) => center.active)
  const studentCenters = activeCenters.filter((center) => capabilities?.student_cost_center_ids.includes(center.id))
  const managedCenters = activeCenters.filter((center) => capabilities?.managed_cost_center_ids.includes(center.id))

  const tabs = useMemo(() => {
    const items: Array<{ key: WorkbenchTab; label: string; icon: typeof GraduationCap }> = [
      { key: 'overview', label: 'Overview', icon: GraduationCap },
    ]
    if (capabilities?.student) items.push({ key: 'submissions', label: 'My submissions', icon: FileUp })
    if (capabilities?.manager || capabilities?.tenant_admin) {
      items.push({ key: 'review', label: 'Review queue', icon: ClipboardCheck })
    }
    if (capabilities?.tenant_admin) items.push({ key: 'centers', label: 'Classes & clubs', icon: Building2 })
    if (capabilities?.tenant_admin) items.push({ key: 'classroom', label: 'Google Classroom', icon: BookOpen })
    return items
  }, [capabilities])

  useEffect(() => {
    if (!tabs.some((tab) => tab.key === activeTab)) setActiveTab('overview')
  }, [activeTab, tabs])

  useEffect(() => {
    if (!capabilities?.tenant_admin || !new URLSearchParams(window.location.search).has('classroom')) return
    setActiveTab('classroom')
    window.history.replaceState({}, '', window.location.pathname)
  }, [capabilities?.tenant_admin])

  if (capabilitiesQuery.isLoading || modeQuery.isLoading) {
    return <div className="p-4 md:p-6 text-sm text-[var(--brand-text-secondary)]">Loading Education workspace…</div>
  }

  if (!license.hasFeature('education_workflows') || !capabilities?.education_enabled) {
    return (
      <div className="p-4 md:p-6">
        <PageHeader icon={GraduationCap} title="Education" subtitle="Classroom print review and cost-center access" />
        <Card>
          <EmptyState
            icon={ShieldCheck}
            title="Education entitlement required"
            description="This installation does not currently report an active Education workflow entitlement."
          />
        </Card>
      </div>
    )
  }

  if (!modeQuery.data?.enabled) {
    return (
      <div className="p-4 md:p-6">
        <PageHeader icon={GraduationCap} title="Education" subtitle="Classroom print review and cost-center access" />
        <Card>
          <EmptyState
            icon={BookOpenCheck}
            title="Education mode is off"
            description={capabilities.tenant_admin
              ? 'Enable Education mode in Settings to expose the classroom workflow.'
              : 'Ask an administrator to enable Education mode for this installation.'}
          />
        </Card>
      </div>
    )
  }

  const visibleCenters: EducationCostCenter[] = capabilities.tenant_admin
    ? activeCenters
    : activeCenters.filter((center) => (
        capabilities.student_cost_center_ids.includes(center.id)
        || capabilities.managed_cost_center_ids.includes(center.id)
      ))

  return (
    <div className="p-4 md:p-6">
      <PageHeader
        icon={GraduationCap}
        title="Education"
        subtitle="Classes, student submissions, teacher review, and authorized printers"
      >
        {capabilities.student && studentCenters.length > 0 && (
          <Button icon={FileUp} onClick={() => setUploadOpen(true)}>Submit print</Button>
        )}
      </PageHeader>

      <div className="mb-5 overflow-x-auto pb-1">
        <TabBar
          tabs={tabs}
          activeTab={activeTab}
          onTabChange={(key) => setActiveTab(key as WorkbenchTab)}
          variant="inline"
        />
      </div>

      {activeTab === 'overview' && (
        <Overview
          capabilities={capabilities}
          activeCenters={activeCenters}
          studentCenters={studentCenters}
          managedCenters={managedCenters}
          onSubmit={() => setUploadOpen(true)}
          onReview={() => setActiveTab('review')}
          onManage={() => setActiveTab('centers')}
        />
      )}

      {activeTab === 'submissions' && (
        <SubmissionQueue centers={studentCenters} reviewable={false} mineOnly />
      )}

      {activeTab === 'review' && (
        <SubmissionQueue
          centers={capabilities.tenant_admin ? activeCenters : managedCenters}
          reviewable
        />
      )}

      {activeTab === 'centers' && capabilities.tenant_admin && (
        <CostCenterManager
          centers={centers}
          includeArchived={includeArchived}
          onIncludeArchivedChange={setIncludeArchived}
          loading={centersQuery.isLoading || centersQuery.isFetchingNextPage}
        />
      )}

      {activeTab === 'classroom' && capabilities.tenant_admin && (
        <ClassroomManager centers={centers} />
      )}

      <SubmissionUploadModal
        open={uploadOpen}
        onClose={() => setUploadOpen(false)}
        centers={studentCenters}
        onCreated={() => {
          queryClient.invalidateQueries({ queryKey: ['education-submissions'] })
          queryClient.invalidateQueries({ queryKey: ['education-centers'] })
          setActiveTab('submissions')
        }}
      />
    </div>
  )
}

function Overview({
  capabilities,
  activeCenters,
  studentCenters,
  managedCenters,
  onSubmit,
  onReview,
  onManage,
}: {
  capabilities: EducationCapabilities
  activeCenters: EducationCostCenter[]
  studentCenters: EducationCostCenter[]
  managedCenters: EducationCostCenter[]
  onSubmit: () => void
  onReview: () => void
  onManage: () => void
}) {
  const activeGrants = activeCenters.reduce((sum, center) => sum + center.counts.active_grants, 0)
  const activePrinters = activeCenters.reduce((sum, center) => sum + center.counts.active_printers, 0)
  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatCard label="Active centers" value={activeCenters.length} icon={Building2} />
        <StatCard label="Roster grants" value={activeGrants} icon={UsersRound} />
        <StatCard label="Printer grants" value={activePrinters} icon={Printer} />
        <StatCard
          label="Your access"
          value={capabilities.tenant_admin ? 'Admin' : capabilities.manager ? 'Manager' : 'Student'}
          icon={ShieldCheck}
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        {capabilities.student && (
          <ActionCard
            icon={FileUp}
            title="Submit a sliced print"
            description={`Upload a Bambu .3mf to ${studentCenters.length} available class${studentCenters.length === 1 ? '' : 'es or clubs'}.`}
            action="Submit print"
            onClick={onSubmit}
            disabled={studentCenters.length === 0}
          />
        )}
        {(capabilities.manager || capabilities.tenant_admin) && (
          <ActionCard
            icon={ClipboardCheck}
            title="Review student work"
            description={capabilities.tenant_admin
              ? 'Review submissions across every active cost center.'
              : `Review submissions for ${managedCenters.length} managed center${managedCenters.length === 1 ? '' : 's'}.`}
            action="Open review queue"
            onClick={onReview}
          />
        )}
        {capabilities.tenant_admin && (
          <ActionCard
            icon={Building2}
            title="Manage classes and clubs"
            description="Assign students, managers, and authorized printers with revision-safe updates."
            action="Manage cost centers"
            onClick={onManage}
          />
        )}
      </div>
      {capabilities.tenant_admin && <ReadinessPanel />}
    </div>
  )
}

function ActionCard({
  icon: Icon,
  title,
  description,
  action,
  onClick,
  disabled = false,
}: {
  icon: typeof GraduationCap
  title: string
  description: string
  action: string
  onClick: () => void
  disabled?: boolean
}) {
  return (
    <Card className="flex min-h-48 flex-col border border-[var(--brand-card-border)]">
      <Icon size={20} className="text-[var(--brand-primary)]" />
      <h2 className="mt-4 text-sm font-semibold text-[var(--brand-text-primary)]">{title}</h2>
      <p className="mt-2 flex-1 text-xs leading-relaxed text-[var(--brand-text-secondary)]">{description}</p>
      <Button className="mt-5 w-full" variant="secondary" onClick={onClick} disabled={disabled}>{action}</Button>
    </Card>
  )
}
