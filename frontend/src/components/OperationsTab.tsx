import { useEffect, useRef, useState } from 'react'
import { ApiError } from '../api/client'
import { getOpsJob, getOpsJobs, runOpsCommand } from '../api/opsClient'
import type { CleanupMode, OpsCommand, OpsJob, OpsJobDetail, OpsRunOptions } from '../api/opsTypes'
import { useT } from '../i18n'

interface OperationsTabProps {
  slug: string
  adminToken: string
  onUnauthorized: () => void
}

type JobsById = Record<string, OpsJobDetail | OpsJob>

function isActive(status: string): boolean {
  return status === 'queued' || status === 'running'
}

function JobStatusBadge({ status }: { status: string }) {
  const { t } = useT()
  const label = (t.operator.jobStatus as Record<string, string>)[status] ?? status
  return <span className={`badge badge-ops-${status}`}>{label}</span>
}

function ProgressBar({ done, total }: { done: number | null; total: number | null }) {
  if (!total || total <= 0) return null
  const pct = Math.min(100, Math.round(((done ?? 0) / total) * 100))
  return (
    <div className="ops-progress">
      <div className="ops-progress-bar" style={{ width: `${pct}%` }} />
      <span className="ops-progress-label">
        {done ?? 0} / {total}
      </span>
    </div>
  )
}

function JobPanel({ job }: { job: OpsJobDetail | OpsJob }) {
  const { t } = useT()
  const [showResult, setShowResult] = useState(false)
  const result = 'result' in job ? job.result : undefined
  return (
    <div className="ops-job-panel">
      <div className="ops-job-panel-header">
        <JobStatusBadge status={job.status} />
        {job.message && <span className="muted">{job.message}</span>}
      </div>
      {isActive(job.status) && <ProgressBar done={job.progress_done} total={job.progress_total} />}
      {job.error && <p className="error-text">{job.error}</p>}
      {!isActive(job.status) && result !== undefined && (
        <div>
          <button type="button" className="btn-link" onClick={() => setShowResult((v) => !v)}>
            {t.operator.resultLabel}
          </button>
          {showResult && <pre className="ops-result-json">{JSON.stringify(result, null, 2)}</pre>}
        </div>
      )}
    </div>
  )
}

function ConfirmDialog({
  title,
  message,
  onCancel,
  onConfirm,
}: {
  title: string
  message: string
  onCancel: () => void
  onConfirm: () => void
}) {
  const { t } = useT()
  return (
    <div className="modal-overlay" role="dialog" aria-modal="true">
      <div className="modal-card">
        <h2>{title}</h2>
        <p>{message}</p>
        <div className="modal-actions">
          <button type="button" className="btn btn-secondary" onClick={onCancel}>
            {t.operator.confirmCancel}
          </button>
          <button type="button" className="btn btn-danger" onClick={onConfirm}>
            {t.operator.confirmProceed}
          </button>
        </div>
      </div>
    </div>
  )
}

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

  const [wipeAdvancedOpen, setWipeAdvancedOpen] = useState(false)
  const [wipeConfirmText, setWipeConfirmText] = useState('')

  const [pendingConfirm, setPendingConfirm] = useState<{
    title: string
    message: string
    onConfirm: () => void
  } | null>(null)

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
      setHistoryError(err instanceof Error ? err.message : 'Failed to load job history.')
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
          [command]: err instanceof Error ? err.message : 'Failed to run command.',
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
  const syncJob = latestJobFor('sync')

  return (
    <div className="ops-tab">
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

      <div className="ops-card">
        <h3>{t.operator.commands.status.title}</h3>
        <p className="muted">{t.operator.commands.status.description}</p>
        {runError.status && <p className="error-text">{runError.status}</p>}
        <button type="button" className="btn btn-primary" onClick={() => launch('status')}>
          {t.operator.runButton}
        </button>
        {statusJob && <JobPanel job={statusJob} />}
      </div>

      <div className="ops-card">
        <h3>{t.operator.commands.import.title}</h3>
        <p className="muted">{t.operator.commands.import.description}</p>
        {runError.import && <p className="error-text">{runError.import}</p>}
        <label className="ops-toggle">
          <input type="checkbox" checked={importDryRun} onChange={(e) => setImportDryRun(e.target.checked)} />
          {t.operator.dryRun}
        </label>
        <label className="ops-toggle">
          <input
            type="checkbox"
            checked={importOnlyWithStock}
            onChange={(e) => setImportOnlyWithStock(e.target.checked)}
          />
          {t.operator.onlyWithStock}
        </label>
        <label className="ops-toggle">
          <input type="checkbox" checked={importPublish} onChange={(e) => setImportPublish(e.target.checked)} />
          {t.operator.publish}
        </label>
        <label className="ops-field">
          {t.operator.limit}
          <input
            type="number"
            min={0}
            value={importLimit}
            onChange={(e) => setImportLimit(e.target.value)}
          />
        </label>
        <button
          type="button"
          className="btn btn-primary"
          onClick={() => {
            const options: OpsRunOptions = {
              apply: !importDryRun,
              only_with_stock: importOnlyWithStock,
              publish: importPublish,
              ...(importLimit ? { limit: Number(importLimit) } : {}),
            }
            if (!importDryRun) {
              setPendingConfirm({
                title: t.operator.confirmTitle,
                message: t.operator.commands.import.confirmApply,
                onConfirm: () => launch('import', options),
              })
            } else {
              launch('import', options)
            }
          }}
        >
          {t.operator.runButton}
        </button>
        {importJob && <JobPanel job={importJob} />}
      </div>

      <div className="ops-card">
        <h3>{t.operator.commands.dedupe.title}</h3>
        <p className="muted">{t.operator.commands.dedupe.description}</p>
        {runError.dedupe && <p className="error-text">{runError.dedupe}</p>}
        <label className="ops-toggle">
          <input type="checkbox" checked={dedupeDryRun} onChange={(e) => setDedupeDryRun(e.target.checked)} />
          {t.operator.dryRun}
        </label>
        <button type="button" className="btn-link" onClick={() => setDedupeAdvancedOpen((v) => !v)}>
          {t.operator.advancedDisclosure}
        </button>
        {dedupeAdvancedOpen && (
          <div className="ops-danger-zone">
            <label className="ops-toggle">
              <input type="checkbox" checked={dedupeForce} onChange={(e) => setDedupeForce(e.target.checked)} />
              {t.operator.force}
            </label>
            <p className="error-text">{t.operator.forceWarning}</p>
          </div>
        )}
        <button
          type="button"
          className="btn btn-primary"
          onClick={() => {
            const options: OpsRunOptions = { apply: !dedupeDryRun, force: dedupeForce }
            if (!dedupeDryRun) {
              setPendingConfirm({
                title: t.operator.confirmTitle,
                message: t.operator.commands.dedupe.confirmApply,
                onConfirm: () => launch('dedupe', options),
              })
            } else {
              launch('dedupe', options)
            }
          }}
        >
          {t.operator.runButton}
        </button>
        {dedupeJob && <JobPanel job={dedupeJob} />}
      </div>

      <div className="ops-card">
        <h3>{t.operator.commands.rekey.title}</h3>
        <p className="muted">{t.operator.commands.rekey.description}</p>
        {runError.rekey && <p className="error-text">{runError.rekey}</p>}
        <label className="ops-toggle">
          <input type="checkbox" checked={rekeyDryRun} onChange={(e) => setRekeyDryRun(e.target.checked)} />
          {t.operator.dryRun}
        </label>
        <label className="ops-toggle">
          <input
            type="checkbox"
            checked={rekeyAutoResolve}
            onChange={(e) => setRekeyAutoResolve(e.target.checked)}
          />
          {t.operator.autoResolve}
        </label>
        <button
          type="button"
          className="btn btn-primary"
          onClick={() => {
            const options: OpsRunOptions = { apply: !rekeyDryRun, auto_resolve: rekeyAutoResolve }
            if (!rekeyDryRun) {
              setPendingConfirm({
                title: t.operator.confirmTitle,
                message: t.operator.commands.rekey.confirmApply,
                onConfirm: () => launch('rekey', options),
              })
            } else {
              launch('rekey', options)
            }
          }}
        >
          {t.operator.runButton}
        </button>
        {rekeyJob && <JobPanel job={rekeyJob} />}
      </div>

      <div className="ops-card">
        <h3>{t.operator.commands.fixTracking.title}</h3>
        <p className="muted">{t.operator.commands.fixTracking.description}</p>
        {runError.fix_tracking && <p className="error-text">{runError.fix_tracking}</p>}
        <label className="ops-toggle">
          <input
            type="checkbox"
            checked={fixTrackingDryRun}
            onChange={(e) => setFixTrackingDryRun(e.target.checked)}
          />
          {t.operator.dryRun}
        </label>
        <button
          type="button"
          className="btn btn-primary"
          onClick={() => {
            const options: OpsRunOptions = { apply: !fixTrackingDryRun }
            if (!fixTrackingDryRun) {
              setPendingConfirm({
                title: t.operator.confirmTitle,
                message: t.operator.commands.fixTracking.confirmApply,
                onConfirm: () => launch('fix_tracking', options),
              })
            } else {
              launch('fix_tracking', options)
            }
          }}
        >
          {t.operator.runButton}
        </button>
        {fixTrackingJob && <JobPanel job={fixTrackingJob} />}
      </div>

      <div className="ops-card">
        <h3>{t.operator.commands.sync.title}</h3>
        <p className="muted">{t.operator.commands.sync.description}</p>
        {runError.sync && <p className="error-text">{runError.sync}</p>}
        <label className="ops-toggle">
          <input type="checkbox" checked={syncDryRun} onChange={(e) => setSyncDryRun(e.target.checked)} />
          {t.operator.dryRun}
        </label>
        <label className="ops-toggle">
          <input type="checkbox" checked={syncFull} onChange={(e) => setSyncFull(e.target.checked)} />
          {t.operator.full}
        </label>
        <button type="button" className="btn-link" onClick={() => setSyncAdvancedOpen((v) => !v)}>
          {t.operator.advancedDisclosure}
        </button>
        {syncAdvancedOpen && (
          <div className="ops-danger-zone">
            <label className="ops-toggle">
              <input type="checkbox" checked={syncForce} onChange={(e) => setSyncForce(e.target.checked)} />
              {t.operator.force}
            </label>
            <p className="error-text">{t.operator.syncForceWarning}</p>
          </div>
        )}
        <button
          type="button"
          className="btn btn-primary"
          onClick={() => {
            const options: OpsRunOptions = { dry_run: syncDryRun, full: syncFull, force: syncForce }
            if (!syncDryRun) {
              setPendingConfirm({
                title: t.operator.confirmTitle,
                message: t.operator.commands.sync.confirmApply,
                onConfirm: () => launch('sync', options),
              })
            } else {
              launch('sync', options)
            }
          }}
        >
          {t.operator.runButton}
        </button>
        {syncJob && <JobPanel job={syncJob} />}
      </div>

      <div className="ops-card">
        <h3>{t.operator.commands.cleanupNoStock.title}</h3>
        <p className="muted">{t.operator.commands.cleanupNoStock.description}</p>
        {runError.cleanup_no_stock && <p className="error-text">{runError.cleanup_no_stock}</p>}
        <label className="ops-toggle">
          <input
            type="checkbox"
            checked={cleanupDryRun}
            onChange={(e) => setCleanupDryRun(e.target.checked)}
          />
          {t.operator.dryRun}
        </label>
        <label className="ops-field">
          {t.operator.cleanupMode}
          <select value={cleanupMode} onChange={(e) => setCleanupMode(e.target.value as CleanupMode)}>
            <option value="draft">{t.operator.cleanupModeDraft}</option>
            <option value="delete">{t.operator.cleanupModeDelete}</option>
          </select>
        </label>
        {cleanupMode === 'delete' && <p className="error-text">{t.operator.forceWarning}</p>}
        <button
          type="button"
          className="btn-link"
          onClick={() => setCleanupAdvancedOpen((v) => !v)}
        >
          {t.operator.advancedDisclosure}
        </button>
        {cleanupAdvancedOpen && (
          <div className="ops-danger-zone">
            <label className="ops-toggle">
              <input
                type="checkbox"
                checked={cleanupForce}
                onChange={(e) => setCleanupForce(e.target.checked)}
              />
              {t.operator.force}
            </label>
            <p className="error-text">{t.operator.forceWarning}</p>
          </div>
        )}
        <button
          type="button"
          className={cleanupMode === 'delete' && !cleanupDryRun ? 'btn btn-danger' : 'btn btn-primary'}
          onClick={() => {
            const options: OpsRunOptions = {
              apply: !cleanupDryRun,
              mode: cleanupMode,
              force: cleanupForce,
            }
            if (!cleanupDryRun) {
              setPendingConfirm({
                title: t.operator.confirmTitle,
                message:
                  cleanupMode === 'delete'
                    ? t.operator.commands.cleanupNoStock.confirmApplyDelete
                    : t.operator.commands.cleanupNoStock.confirmApplyDraft,
                onConfirm: () => launch('cleanup_no_stock', options),
              })
            } else {
              launch('cleanup_no_stock', options)
            }
          }}
        >
          {t.operator.runButton}
        </button>
        {cleanupJob && <JobPanel job={cleanupJob} />}
      </div>

      <div className="ops-card ops-card-danger">
        <button type="button" className="btn-link" onClick={() => setWipeAdvancedOpen((v) => !v)}>
          {t.operator.advancedDisclosure}
        </button>
        {wipeAdvancedOpen && (
          <div className="ops-danger-zone">
            <h3>{t.operator.commands.wipe.title}</h3>
            <p className="muted">{t.operator.commands.wipe.description}</p>
            {runError.wipe && <p className="error-text">{runError.wipe}</p>}
            <label className="ops-field">
              {t.operator.typeDomainToConfirm(slug)}
              <input
                type="text"
                value={wipeConfirmText}
                onChange={(e) => setWipeConfirmText(e.target.value)}
              />
            </label>
            <button
              type="button"
              className="btn btn-danger"
              disabled={wipeConfirmText.trim() !== slug}
              onClick={() => {
                if (wipeConfirmText.trim() !== slug) return
                setPendingConfirm({
                  title: t.operator.confirmTitle,
                  message: t.operator.commands.wipe.confirmApply,
                  onConfirm: () => launch('wipe', { confirm: true }),
                })
              }}
            >
              {t.operator.runButton}
            </button>
            {wipeJob && <JobPanel job={wipeJob} />}
          </div>
        )}
      </div>

      <div className="ops-card">
        <h3>{t.operator.jobHistoryTitle}</h3>
        {historyError && <p className="error-text">{historyError}</p>}
        {jobOrder.length === 0 ? (
          <p className="empty-state">{t.operator.noJobs}</p>
        ) : (
          <table className="ops-history-table">
            <thead>
              <tr>
                <th>{t.operator.columns.command}</th>
                <th>{t.operator.columns.status}</th>
                <th>{t.operator.columns.time}</th>
              </tr>
            </thead>
            <tbody>
              {jobOrder.map((id) => {
                const job = jobs[id]
                if (!job) return null
                return (
                  <JobHistoryRow
                    key={id}
                    job={job}
                    onExpand={async () => {
                      if ('result' in job) return
                      try {
                        const detail = await getOpsJob(adminToken, slug, id)
                        setJobs((prev) => ({ ...prev, [id]: detail }))
                      } catch (err) {
                        handleUnauthorizedOnce(err)
                      }
                    }}
                  />
                )
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}

function JobHistoryRow({ job, onExpand }: { job: OpsJobDetail | OpsJob; onExpand: () => void }) {
  const [expanded, setExpanded] = useState(false)
  const result = 'result' in job ? job.result : undefined
  return (
    <>
      <tr
        className="ops-history-row"
        onClick={() => {
          setExpanded((v) => !v)
          onExpand()
        }}
      >
        <td>{job.command}</td>
        <td>
          <JobStatusBadge status={job.status} />
        </td>
        <td>{new Date(job.created_at).toLocaleString()}</td>
      </tr>
      {expanded && (
        <tr>
          <td colSpan={3}>
            {job.message && <p className="muted">{job.message}</p>}
            {job.error && <p className="error-text">{job.error}</p>}
            {result !== undefined && <pre className="ops-result-json">{JSON.stringify(result, null, 2)}</pre>}
          </td>
        </tr>
      )}
    </>
  )
}
