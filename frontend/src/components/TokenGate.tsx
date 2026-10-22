import { useState } from 'react'
import type { FormEvent } from 'react'
import { useT } from '../i18n'
import { AuthLayout } from './AuthLayout'
import { Field } from './Field'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'

interface TokenGateProps {
  slug: string
  onSubmit: (token: string) => void
  onBackToLogin?: () => void
}

export function TokenGate({ slug, onSubmit, onBackToLogin }: TokenGateProps) {
  const { t } = useT()
  const [value, setValue] = useState('')

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const trimmed = value.trim()
    if (trimmed) onSubmit(trimmed)
  }

  return (
    <AuthLayout title={t.login.title} description={t.login.accessKeyPrompt(slug)}>
      <form className="flex flex-col gap-4" onSubmit={handleSubmit}>
        <Field label={t.login.accessKeyLabel}>
          {(control) => (
            <Input
              {...control}
              className="h-11 sm:h-9"
              type="password"
              autoFocus
              placeholder={t.login.accessKeyPlaceholder}
              value={value}
              onChange={(event) => setValue(event.target.value)}
            />
          )}
        </Field>
        <Button type="submit" className="h-11 sm:h-9" disabled={!value.trim()}>
          {t.common.continueLabel}
        </Button>
        {onBackToLogin && (
          <Button type="button" variant="link" className="h-11 sm:h-9" onClick={onBackToLogin}>
            {t.common.backToLogin}
          </Button>
        )}
      </form>
    </AuthLayout>
  )
}
