import { useState } from 'react'
import type { FormEvent } from 'react'
import { ApiError, login } from '../api/client'

interface LoginCardProps {
  slug: string
  onSuccess: (token: string) => void
  onUseAccessKey: () => void
}

export function LoginCard({ slug, onSuccess, onUseAccessKey }: LoginCardProps) {
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
        setError('Too many login attempts. Please wait a minute and try again.')
      } else if (err instanceof ApiError && err.status === 401) {
        setError('Invalid email or password.')
      } else {
        setError(err instanceof Error ? err.message : 'Login failed.')
      }
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="centered-page">
      <form className="token-card" onSubmit={handleSubmit}>
        <h1>Store portal</h1>
        <p className="muted">Sign in to &ldquo;{slug}&rdquo; with your email and password.</p>
        <input
          type="email"
          autoFocus
          placeholder="Email"
          value={email}
          autoComplete="username"
          onChange={(event) => setEmail(event.target.value)}
        />
        <input
          type="password"
          placeholder="Password"
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
          {submitting ? 'Signing in…' : 'Sign in'}
        </button>
        <button type="button" className="btn-link" onClick={onUseAccessKey}>
          Have an access key?
        </button>
      </form>
    </div>
  )
}
