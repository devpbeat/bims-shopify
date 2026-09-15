import { useState } from 'react'
import type { FormEvent } from 'react'
import { ApiError, login } from '../api/client'
import { useT } from '../i18n'
import { LanguageToggle } from './LanguageToggle'

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
    <div className="centered-page">
      <form className="token-card" onSubmit={handleSubmit}>
        <div className="token-card-top">
          <h1>{t.login.title}</h1>
          <LanguageToggle />
        </div>
        <p className="muted">{t.login.subtitle(slug)}</p>
        <input
          type="email"
          autoFocus
          placeholder={t.login.emailPlaceholder}
          value={email}
          autoComplete="username"
          onChange={(event) => setEmail(event.target.value)}
        />
        <input
          type="password"
          placeholder={t.login.passwordPlaceholder}
          value={password}
          autoComplete="current-password"
          onChange={(event) => setPassword(event.target.value)}
        />
        {error && <p className="error-text">{error}</p>}
        <button
          type="submit"
          className="btn btn-primary"
          disabled={submitting || !email.trim() || !password}
        >
          {submitting ? t.common.signingIn : t.common.signIn}
        </button>
        <button type="button" className="btn-link" onClick={onUseAccessKey}>
          {t.common.haveAccessKey}
        </button>
      </form>
    </div>
  )
}
