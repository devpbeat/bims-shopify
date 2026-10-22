import { useEffect, useRef, useState } from 'react'
import { ApiError } from '../api/client'
import { getOpsJob, getOpsJobs, runOpsCommand } from '../api/opsClient'
import type { CleanupMode, OpsCommand, OpsJob, OpsJobDetail, OpsRunOptions } from '../api/opsTypes'
import { useT } from '../i18n'
import { Field } from './Field'
import { ConfirmDialog, Disclosure, isActive, JobDetailsDialog, JobPanel, JobStatusBadge, OpsCard, ToggleField } from './OpsParts'
import { ResponsiveRecords } from './ResponsiveRecords'
import type { RecordColumn } from './ResponsiveRecords'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

interface OperationsTabProps {
  slug: string
  adminToken: string
  onUnauthorized: () => void
}

type JobsById = Record<string, OpsJobDetail | OpsJob>

const touch = 'h-11 sm:h-9'

export function OperationsTab({ slug, adminToken, onUnauthorized }: OperationsTabProps) {
  const { t } = useT()
  const [jobs, setJobs] = useState<JobsById>({})
  const [jobOrder, setJobOrder] = useState<string[]>([])
  const [historyError, setHistoryError] = useState<string | null>(null)
  const pollRef = useRef<number | null>(null)

  const [importDryRun, setImportDryRun] = useState(true)
  const [importOnlyWithStock, setImportOnlyWithStock] = useState(false)
  const [importPublish, setImportPublish] = useState(false)
  const [importLimit, setImportLimit] = useState('')

  const [dedupeDryRun, setDedupeDryRun] = useState(true)
  const [dedupeForce, setDedupeForce] = useState(false)
  const [dedupeAdvancedOpen, setDedupeAdvancedOpen] = useState(false)

  const [rekeyDryRun, setRekeyDryRun] = useState(true)
  const [rekeyAutoResolve, setRekeyAutoResolve] = useState(false)

  const [fixTrackingDryRun, setFixTrackingDryRun] = useState(true)

  const [syncDryRun, setSyncDryRun] = useState(true)
  const [syncFull, setSyncFull] = useState(false)
  const [syncForce, setSyncForce] = useState(false)
  const [syncAdvancedOpen, setSyncAdvancedOpen] = useState(false)

  const [cleanupDryRun, setCleanupDryRun] = useState(true)
  const [cleanupMode, setCleanupMode] = useState<CleanupMode>('draft')
  const [cleanupForce, setCleanupForce] = useState(false)
  const [cleanupAdvancedOpen, setCleanupAdvancedOpen] = useState(false)

  const [publishDryRun, setPublishDryRun] = useState(true)
  const [publishLimit, setPublishLimit] = useState('')
  const [publishRequireImages, setPublishRequireImages] = useState(true)

  const [refreshDryRun, setRefreshDryRun] = useState(true)
  const [refreshOnlyWithStock, setRefreshOnlyWithStock] = useState(false)
  const [refreshRequireImages, setRefreshRequireImages] = useState(true)
  const [refreshLimit, setRefreshLimit] = useState('')

  const [diagnoseQuery, setDiagnoseQuery] = useState('')

  const [wipeAdvancedOpen, setWipeAdvancedOpen] = useState(false)
  const [wipeConfirmText, setWipeConfirmText] = useState('')

  const [pendingConfirm, setPendingConfirm] = useState<{
    title: string
    message: string
    onConfirm: () => void
  } | null>(null)

  const [detailsJobId, setDetailsJobId] = useState<string | null>(null)

  const [runError, setRunError] = useState<Partial<Record<OpsCommand, string>>>({})

  function handleUnauthorizedOnce(err: unknown): boolean {
    if (err instanceof ApiError && err.status === 401) {
      onUnauthorized()
      return true
    }
    return false
  }

  async function refreshHistory() {
    try {
      const list = await getOpsJobs(adminToken, slug, 20)
      setJobs((prev) => {
        const next = { ...prev }
        for (const job of list) next[job.id] = next[job.id] ?? job
        return next
      })
      setJobOrder(list.map((job) => job.id))
      setHistoryError(null)
    } catch (err) {
      if (handleUnauthorizedOnce(err)) return
      setHistoryError(err instanceof Error ? err.message : t.operator.historyLoadFailed)
    }
  }

  useEffect(() => {
    refreshHistory()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug, adminToken])

  useEffect(() => {
    const activeIds = jobOrder.filter((id) => isActive(jobs[id]?.status))
    if (activeIds.length === 0) {
      if (pollRef.current) {
        window.clearInterval(pollRef.current)
        pollRef.current = null
      }
      return
    }
    if (pollRef.current) return
    pollRef.current = window.setInterval(async () => {
      const ids = jobOrder.filter((id) => isActive(jobs[id]?.status))
      for (const id of ids) {
        try {
          const detail = await getOpsJob(adminToken, slug, id)
          setJobs((prev) => ({ ...prev, [id]: detail }))
        } catch (err) {
          if (handleUnauthorizedOnce(err)) return
        }
      }
      if (ids.length === 0) refreshHistory()
    }, 2000)
    return () => {
      if (pollRef.current) {
        window.clearInterval(pollRef.current)
        pollRef.current = null
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobOrder, jobs])

  async function launch(command: OpsCommand, options?: OpsRunOptions) {
    setRunError((prev) => ({ ...prev, [command]: undefined }))
    try {
      const { job_id } = await runOpsCommand(adminToken, slug, command, options)
      const detail = await getOpsJob(adminToken, slug, job_id)
      setJobs((prev) => ({ ...prev, [job_id]: detail }))
      setJobOrder((prev) => [job_id, ...prev.filter((id) => id !== job_id)])
    } catch (err) {
      if (handleUnauthorizedOnce(err)) return
      if (err instanceof ApiError && err.status === 409) {
        setRunError((prev) => ({ ...prev, [command]: t.operator.jobAlreadyRunning }))
      } else {
        setRunError((prev) => ({
          ...prev,
          [command]: err instanceof Error ? err.message : t.operator.runFailed,
        }))
      }
    }
  }

  function latestJobFor(command: OpsCommand): OpsJobDetail | OpsJob | undefined {
    const id = jobOrder.find((jobId) => jobs[jobId]?.command === command)
    return id ? jobs[id] : undefined
  }

  const statusJob = latestJobFor('status')
  const importJob = latestJobFor('import')
  const dedupeJob = latestJobFor('dedupe')
  const wipeJob = latestJobFor('wipe')
  const rekeyJob = latestJobFor('rekey')
  const fixTrackingJob = latestJobFor('fix_tracking')
  const cleanupJob = latestJobFor('cleanup_no_stock')
  const publishJob = latestJobFor('publish')
  const diagnoseJob = latestJobFor('diagnose')
  const syncJob = latestJobFor('sync')
  const refreshJob = latestJobFor('refresh')
  const op = t.operator

  /** Runs right away, or asks for confirmation first when `confirmMessage` is given. */
  function run(command: OpsCommand, options?: OpsRunOptions, confirmMessage?: string) {
    if (confirmMessage) {
      setPendingConfirm({ title: op.confirmTitle, message: confirmMessage, onConfirm: () => launch(command, options) })
    } else {
      launch(command, options)
    }
  }

  const runButton = (onClick: () => void, opts: { disabled?: boolean; destructive?: boolean } = {}) => (
    <Button type="button" variant={opts.destructive ? 'destructive' : 'default'} className={`${touch} w-full sm:w-auto sm:self-start`} disabled={opts.disabled} onClick={onClick}>
      {op.runButton}
    </Button>
  )

  const limitField = (value: string, onChange: (value: string) => void) => (
    <Field label={op.limit}>
      {(control) => <Input {...control} className={`${touch} sm:w-40`} type="number" inputMode="numeric" min={0} value={value} onChange={(e) => onChange(e.target.value)} />}
    </Field>
  )

  const historyColumns: RecordColumn<OpsJobDetail | OpsJob>[] = [
    { id: 'command', header: op.columns.command, cell: (job) => job.command },
    { id: 'status', header: op.columns.status, cell: (job) => <JobStatusBadge status={job.status} /> },
    { id: 'time', header: op.columns.time, cell: (job) => <span className="text-muted-foreground">{new Date(job.created_at).toLocaleString()}</span> },
  ]

  async function openDetails(id: string) {
    setDetailsJobId(id)
    if ('result' in jobs[id]) return
    try {
      const detail = await getOpsJob(adminToken, slug, id)
      setJobs((prev) => ({ ...prev, [id]: detail }))
    } catch (err) {
      handleUnauthorizedOnce(err)
    }
  }

  return (
    <div className="flex flex-col gap-4">
      {pendingConfirm && (
        <ConfirmDialog
          title={pendingConfirm.title}
          message={pendingConfirm.message}
          onCancel={() => setPendingConfirm(null)}
          onConfirm={() => {
            const action = pendingConfirm.onConfirm
            setPendingConfirm(null)
            action()
          }}
        />
      )}

      <OpsCard title={op.commands.status.title} description={op.commands.status.description} error={runError.status} job={statusJob}>
        {runButton(() => run('status'))}
      </OpsCard>

      <OpsCard title={op.commands.refresh.title} description={op.commands.refresh.description} error={runError.refresh} job={refreshJob}>
        <ToggleField label={op.dryRun} checked={refreshDryRun} onChange={setRefreshDryRun} />
        <ToggleField label={op.onlyWithStock} checked={refreshOnlyWithStock} onChange={setRefreshOnlyWithStock} />
        <ToggleField label={op.requireImages} checked={refreshRequireImages} onChange={setRefreshRequireImages} />
        {limitField(refreshLimit, setRefreshLimit)}
        {runButton(() =>
          run(
            'refresh',
            {
              apply: !refreshDryRun,
              only_with_stock: refreshOnlyWithStock,
              require_images: refreshRequireImages,
              ...(refreshLimit ? { limit: Number(refreshLimit) } : {}),
            },
            refreshDryRun ? undefined : op.commands.refresh.confirmApply,
          ),
        )}
      </OpsCard>

      <OpsCard title={op.commands.import.title} description={op.commands.import.description} error={runError.import} job={importJob}>
        <ToggleField label={op.dryRun} checked={importDryRun} onChange={setImportDryRun} />
        <ToggleField label={op.onlyWithStock} checked={importOnlyWithStock} onChange={setImportOnlyWithStock} />
        <ToggleField label={op.publish} checked={importPublish} onChange={setImportPublish} />
        {limitField(importLimit, setImportLimit)}
        {runButton(() =>
          run(
            'import',
            {
              apply: !importDryRun,
              only_with_stock: importOnlyWithStock,
              publish: importPublish,
              ...(importLimit ? { limit: Number(importLimit) } : {}),
            },
            importDryRun ? undefined : op.commands.import.confirmApply,
          ),
        )}
      </OpsCard>

      <OpsCard title={op.commands.dedupe.title} description={op.commands.dedupe.description} error={runError.dedupe} job={dedupeJob}>
        <ToggleField label={op.dryRun} checked={dedupeDryRun} onChange={setDedupeDryRun} />
        <Disclosure label={op.advancedDisclosure} open={dedupeAdvancedOpen} onToggle={() => setDedupeAdvancedOpen((v) => !v)}>
          <ToggleField label={op.force} checked={dedupeForce} onChange={setDedupeForce} />
          <p className="text-sm text-destructive">{op.forceWarning}</p>
        </Disclosure>
        {runButton(() => run('dedupe', { apply: !dedupeDryRun, force: dedupeForce }, dedupeDryRun ? undefined : op.commands.dedupe.confirmApply))}
      </OpsCard>

      <OpsCard title={op.commands.rekey.title} description={op.commands.rekey.description} error={runError.rekey} job={rekeyJob}>
        <ToggleField label={op.dryRun} checked={rekeyDryRun} onChange={setRekeyDryRun} />
        <ToggleField label={op.autoResolve} checked={rekeyAutoResolve} onChange={setRekeyAutoResolve} />
        {runButton(() => run('rekey', { apply: !rekeyDryRun, auto_resolve: rekeyAutoResolve }, rekeyDryRun ? undefined : op.commands.rekey.confirmApply))}
      </OpsCard>

      <OpsCard title={op.commands.fixTracking.title} description={op.commands.fixTracking.description} error={runError.fix_tracking} job={fixTrackingJob}>
        <ToggleField label={op.dryRun} checked={fixTrackingDryRun} onChange={setFixTrackingDryRun} />
        {runButton(() => run('fix_tracking', { apply: !fixTrackingDryRun }, fixTrackingDryRun ? undefined : op.commands.fixTracking.confirmApply))}
      </OpsCard>

      <OpsCard title={op.commands.sync.title} description={op.commands.sync.description} error={runError.sync} job={syncJob}>
        <ToggleField label={op.dryRun} checked={syncDryRun} onChange={setSyncDryRun} />
        <ToggleField label={op.full} checked={syncFull} onChange={setSyncFull} />
        <Disclosure label={op.advancedDisclosure} open={syncAdvancedOpen} onToggle={() => setSyncAdvancedOpen((v) => !v)}>
          <ToggleField label={op.force} checked={syncForce} onChange={setSyncForce} />
          <p className="text-sm text-destructive">{op.syncForceWarning}</p>
        </Disclosure>
        {runButton(() => run('sync', { dry_run: syncDryRun, full: syncFull, force: syncForce }, syncDryRun ? undefined : op.commands.sync.confirmApply))}
      </OpsCard>

      <OpsCard title={op.commands.cleanupNoStock.title} description={op.commands.cleanupNoStock.description} error={runError.cleanup_no_stock} job={cleanupJob}>
        <ToggleField label={op.dryRun} checked={cleanupDryRun} onChange={setCleanupDryRun} />
        <Field label={op.cleanupMode}>
          {(control) => (
            <Select value={cleanupMode} onValueChange={(value) => setCleanupMode(value as CleanupMode)}>
              <SelectTrigger {...control} className={`${touch} w-full sm:w-72`}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="draft">{op.cleanupModeDraft}</SelectItem>
                <SelectItem value="delete">{op.cleanupModeDelete}</SelectItem>
              </SelectContent>
            </Select>
          )}
        </Field>
        {cleanupMode === 'delete' && <p className="text-sm text-destructive">{op.forceWarning}</p>}
        <Disclosure label={op.advancedDisclosure} open={cleanupAdvancedOpen} onToggle={() => setCleanupAdvancedOpen((v) => !v)}>
          <ToggleField label={op.force} checked={cleanupForce} onChange={setCleanupForce} />
          <p className="text-sm text-destructive">{op.forceWarning}</p>
        </Disclosure>
        {runButton(
          () =>
            run(
              'cleanup_no_stock',
              { apply: !cleanupDryRun, mode: cleanupMode, force: cleanupForce },
              cleanupDryRun
                ? undefined
                : cleanupMode === 'delete'
                  ? op.commands.cleanupNoStock.confirmApplyDelete
                  : op.commands.cleanupNoStock.confirmApplyDraft,
            ),
          { destructive: cleanupMode === 'delete' && !cleanupDryRun },
        )}
      </OpsCard>

      <OpsCard title={op.commands.publish.title} description={op.commands.publish.description} error={runError.publish} job={publishJob}>
        <ToggleField label={op.dryRun} checked={publishDryRun} onChange={setPublishDryRun} />
        {limitField(publishLimit, setPublishLimit)}
        <ToggleField label={op.requireImages} checked={publishRequireImages} onChange={setPublishRequireImages} />
        {runButton(() =>
          run(
            'publish',
            {
              apply: !publishDryRun,
              require_images: publishRequireImages,
              ...(publishLimit ? { limit: Number(publishLimit) } : {}),
            },
            publishDryRun
              ? undefined
              : publishRequireImages
                ? op.commands.publish.confirmApply
                : op.commands.publish.confirmApplyNoImages,
          ),
        )}
      </OpsCard>

      <OpsCard title={op.commands.diagnose.title} description={op.commands.diagnose.description} error={runError.diagnose} job={diagnoseJob}>
        <Field label={op.commands.diagnose.queryLabel}>
          {(control) => (
            <Input {...control} className={touch} maxLength={200} placeholder={op.commands.diagnose.queryPlaceholder} value={diagnoseQuery} onChange={(e) => setDiagnoseQuery(e.target.value)} />
          )}
        </Field>
        {runButton(() => run('diagnose', { query: diagnoseQuery.trim() }), { disabled: diagnoseQuery.trim() === '' })}
      </OpsCard>

      <Card size="sm" className="ring-destructive/40">
        <CardContent>
          <Disclosure label={op.advancedDisclosure} open={wipeAdvancedOpen} onToggle={() => setWipeAdvancedOpen((v) => !v)}>
            <h3 className="font-medium">{op.commands.wipe.title}</h3>
            <p className="text-sm text-muted-foreground">{op.commands.wipe.description}</p>
            {runError.wipe && (
              <Alert variant="destructive" role="alert">
                <AlertDescription>{runError.wipe}</AlertDescription>
              </Alert>
            )}
            <Field label={op.typeDomainToConfirm(slug)}>
              {(control) => <Input {...control} className={touch} value={wipeConfirmText} onChange={(e) => setWipeConfirmText(e.target.value)} />}
            </Field>
            {runButton(() => run('wipe', { confirm: true }, op.commands.wipe.confirmApply), {
              destructive: true,
              disabled: wipeConfirmText.trim() !== slug,
            })}
            {wipeJob && <JobPanel job={wipeJob} />}
          </Disclosure>
        </CardContent>
      </Card>

      <Card size="sm">
        <CardHeader>
          <CardTitle>{op.jobHistoryTitle}</CardTitle>
        </CardHeader>
        <CardContent>
          {historyError && (
            <Alert variant="destructive" role="alert">
              <AlertDescription>{historyError}</AlertDescription>
            </Alert>
          )}
          {jobOrder.length === 0 ? (
            <p className="py-4 text-center text-sm text-muted-foreground">{op.noJobs}</p>
          ) : (
            <ResponsiveRecords
              rows={jobOrder.map((id) => jobs[id]).filter((job): job is OpsJobDetail | OpsJob => Boolean(job))}
              columns={historyColumns}
              getKey={(job) => job.id}
              label={op.jobHistoryTitle}
              actionsHeader={t.common.actions}
              actions={(job) => (
                <Button type="button" variant="outline" className={touch} onClick={() => openDetails(job.id)}>
                  {op.viewJobDetails}
                </Button>
              )}
            />
          )}
        </CardContent>
      </Card>

      <JobDetailsDialog job={detailsJobId ? (jobs[detailsJobId] ?? null) : null} onClose={() => setDetailsJobId(null)} />
    </div>
  )
}
