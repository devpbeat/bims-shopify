import { ChevronDownIcon } from 'lucide-react'
import { useId, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { redactPayload } from '../activity/activityFormat'
import type { OpsJob, OpsJobDetail } from '../api/opsTypes'
import { useT } from '../i18n'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Label } from '@/components/ui/label'
import { cn } from '@/lib/utils'

export type AnyJob = OpsJobDetail | OpsJob

export function isActive(status: string): boolean {
  return status === 'queued' || status === 'running'
}

export function JobStatusBadge({ status }: { status: string }) {
  const { t } = useT()
  const label = (t.operator.jobStatus as Record<string, string>)[status] ?? status
  return <Badge variant={status === 'failed' || status === 'interrupted' ? 'destructive' : status === 'succeeded' ? 'secondary' : 'outline'}>{label}</Badge>
}

function ProgressBar({ done, total }: { done: number | null; total: number | null }) {
  const { t } = useT()
  if (!total || total <= 0) return null
  const pct = Math.min(100, Math.round(((done ?? 0) / total) * 100))
  return (
    <div className="flex flex-col gap-1">
      <div
        role="progressbar"
        aria-label={t.operator.progressLabel}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pct}
        className="h-2 overflow-hidden rounded-full bg-muted"
      >
        <div className="h-full bg-primary transition-[width]" style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs text-muted-foreground tabular-nums">
        {done ?? 0} / {total}
      </span>
    </div>
  )
}

/** Redacted, scrollable JSON of a job result. */
export function ResultJson({ value }: { value: unknown }) {
  const text = useMemo(() => JSON.stringify(redactPayload(value), null, 2), [value])
  return (
    <pre
      tabIndex={0}
      className="max-h-64 overflow-auto rounded-md border bg-muted/50 p-3 font-mono text-xs leading-relaxed focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none"
    >
      {text}
    </pre>
  )
}

export function JobPanel({ job }: { job: AnyJob }) {
  const { t } = useT()
  const [showResult, setShowResult] = useState(false)
  const resultId = useId()
  const result = 'result' in job ? job.result : undefined
  return (
    <div className="flex flex-col gap-2 border-t pt-3">
      <div className="flex flex-wrap items-center gap-2">
        <JobStatusBadge status={job.status} />
        {job.message && <span className="text-sm break-words text-muted-foreground">{job.message}</span>}
      </div>
      {isActive(job.status) && <ProgressBar done={job.progress_done} total={job.progress_total} />}
      {job.error && (
        <Alert variant="destructive" role="alert">
          <AlertDescription className="break-words">{job.error}</AlertDescription>
        </Alert>
      )}
      {!isActive(job.status) && result !== undefined && (
        <div className="flex flex-col gap-2">
          <Button
            type="button"
            variant="outline"
            className="h-11 self-start sm:h-9"
            aria-expanded={showResult}
            aria-controls={resultId}
            onClick={() => setShowResult((v) => !v)}
          >
            {t.operator.resultLabel}
          </Button>
          <div id={resultId} hidden={!showResult}>
            {showResult && <ResultJson value={result} />}
          </div>
        </div>
      )}
    </div>
  )
}

interface OpsCardProps {
  title: string
  description?: string
  error?: string
  job?: AnyJob
  danger?: boolean
  children?: ReactNode
}

/** One operator command: form controls, run error and the latest job panel. */
export function OpsCard({ title, description, error, job, danger, children }: OpsCardProps) {
  return (
    <Card size="sm" className={cn(danger && 'ring-destructive/40')}>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        {description && <CardDescription>{description}</CardDescription>}
      </CardHeader>
      <CardContent className="gap-3">
        {error && (
          <Alert variant="destructive" role="alert">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        {children}
        {job && <JobPanel job={job} />}
      </CardContent>
    </Card>
  )
}

interface ToggleFieldProps {
  label: string
  checked: boolean
  onChange: (checked: boolean) => void
}

export function ToggleField({ label, checked, onChange }: ToggleFieldProps) {
  const id = useId()
  return (
    <div className="flex min-h-11 items-center gap-3 sm:min-h-8">
      <Checkbox id={id} checked={checked} onCheckedChange={(value) => onChange(value === true)} />
      <Label htmlFor={id} className="flex-1 py-2">
        {label}
      </Label>
    </div>
  )
}

interface DisclosureProps {
  label: string
  open: boolean
  onToggle: () => void
  children: ReactNode
}

/** "Advanced / danger zone" toggle with correct expanded state for assistive tech. */
export function Disclosure({ label, open, onToggle, children }: DisclosureProps) {
  const id = useId()
  return (
    <div className="flex flex-col gap-3">
      <Button type="button" variant="ghost" className="h-11 justify-start px-2 sm:h-9" aria-expanded={open} aria-controls={id} onClick={onToggle}>
        <ChevronDownIcon className={cn('transition-transform', open && 'rotate-180')} />
        {label}
      </Button>
      <div id={id} hidden={!open}>
        {open && <div className="flex flex-col gap-3 rounded-lg border border-dashed border-destructive/50 p-3">{children}</div>}
      </div>
    </div>
  )
}

interface ConfirmDialogProps {
  title: string
  message: string
  onCancel: () => void
  onConfirm: () => void
}

export function ConfirmDialog({ title, message, onCancel, onConfirm }: ConfirmDialogProps) {
  const { t } = useT()
  return (
    <Dialog open onOpenChange={(open) => !open && onCancel()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{message}</DialogDescription>
        </DialogHeader>
        <DialogFooter className="gap-2">
          <Button type="button" variant="outline" className="h-11 sm:h-9" onClick={onCancel}>
            {t.operator.confirmCancel}
          </Button>
          <Button type="button" variant="destructive" className="h-11 sm:h-9" onClick={onConfirm}>
            {t.operator.confirmProceed}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** Message, error and redacted result of one history job. */
export function JobDetailsDialog({ job, onClose }: { job: AnyJob | null; onClose: () => void }) {
  const { t } = useT()
  const result = job && 'result' in job ? job.result : undefined
  return (
    <Dialog open={job !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="flex max-h-[85dvh] flex-col gap-4 sm:max-w-xl">
        {job && (
          <>
            <DialogHeader>
              <DialogTitle>{job.command}</DialogTitle>
              <DialogDescription>{new Date(job.created_at).toLocaleString()}</DialogDescription>
            </DialogHeader>
            <div className="flex min-h-0 flex-col gap-3 overflow-y-auto">
              <JobStatusBadge status={job.status} />
              {job.message && <p className="text-sm break-words">{job.message}</p>}
              {job.error && (
                <Alert variant="destructive">
                  <AlertDescription className="break-words">{job.error}</AlertDescription>
                </Alert>
              )}
              {result !== undefined ? (
                <>
                  <p className="text-xs text-muted-foreground">{t.activity.rawPayloadNote}</p>
                  <ResultJson value={result} />
                </>
              ) : (
                <p className="text-sm text-muted-foreground">{t.activity.noPayload}</p>
              )}
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}
