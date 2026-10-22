import { useState } from 'react'
import type { FormEvent } from 'react'
import { useT } from '../i18n'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

interface OperatorGateProps {
  slug: string
  error?: string | null
  onSubmit: (adminToken: string) => void
  onClose: () => void
}

export function OperatorGate({ slug, error, onSubmit, onClose }: OperatorGateProps) {
  const { t } = useT()
  const [value, setValue] = useState('')

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const trimmed = value.trim()
    if (trimmed) onSubmit(trimmed)
  }

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <form className="flex flex-col gap-4" onSubmit={handleSubmit}>
          <DialogHeader>
            <DialogTitle>{t.operator.gateTitle}</DialogTitle>
            <DialogDescription>{t.operator.gatePrompt(slug)}</DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-2">
            <Label htmlFor="operator-token">{t.operator.tokenLabel}</Label>
            <Input
              id="operator-token"
              type="password"
              autoComplete="off"
              className="h-11 sm:h-9"
              placeholder={t.operator.tokenPlaceholder}
              value={value}
              aria-invalid={error ? true : undefined}
              aria-describedby={error ? 'operator-token-error' : undefined}
              onChange={(event) => setValue(event.target.value)}
            />
            {error && (
              <p id="operator-token-error" role="alert" className="text-sm text-destructive">
                {error}
              </p>
            )}
          </div>
          <DialogFooter>
            <Button type="submit" className="h-11 sm:h-9" disabled={!value.trim()}>
              {t.operator.enter}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
