import { useState } from 'react'
import type { FormEvent } from 'react'
import { ApiError, login } from '../api/client'
import { useT } from '../i18n'
import { AuthLayout } from './AuthLayout'
import { Field } from './Field'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'

interface LoginCardProps {
  slug: string
  onSuccess: (token: string) => void
  onUseAccessKey: () => void
}

export function LoginCard({ slug, onSuccess, onUseAccessKey }: LoginCardProps) {
  const { t } = useT()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!email.trim() || !password) return
    setSubmitting(true)
    setError(null)
    try {
      const { token } = await login(slug, email.trim(), password)
      onSuccess(token)
    } catch (err) {
      if (err instanceof ApiError && err.status === 429) {
        setError(t.login.tooManyAttempts)
      } else if (err instanceof ApiError && err.status === 401) {
        setError(t.login.invalidCredentials)
      } else {
        setError(err instanceof Error ? err.message : t.login.loginFailedGeneric)
      }
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <AuthLayout title={t.login.title} description={t.login.subtitle(slug)}>
      <form className="flex flex-col gap-4" onSubmit={handleSubmit}>
        <Field label={t.login.emailLabel}>
          {(control) => (
            <Input
              {...control}
              className="h-11 sm:h-9"
              type="email"
              autoFocus
              placeholder={t.login.emailPlaceholder}
              value={email}
              autoComplete="username"
              onChange={(event) => setEmail(event.target.value)}
            />
          )}
        </Field>
        <Field label={t.login.passwordLabel}>
          {(control) => (
            <Input
              {...control}
              className="h-11 sm:h-9"
              type="password"
              placeholder={t.login.passwordPlaceholder}
              value={password}
              autoComplete="current-password"
              onChange={(event) => setPassword(event.target.value)}
            />
          )}
        </Field>
        {error && (
          <Alert variant="destructive" role="alert">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        <Button type="submit" className="h-11 sm:h-9" disabled={submitting || !email.trim() || !password}>
          {submitting ? t.common.signingIn : t.common.signIn}
        </Button>
        <Button type="button" variant="link" className="h-11 sm:h-9" onClick={onUseAccessKey}>
          {t.common.haveAccessKey}
        </Button>
      </form>
    </AuthLayout>
  )
}
