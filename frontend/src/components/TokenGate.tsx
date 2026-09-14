import { useState } from 'react'
import type { FormEvent } from 'react'

interface TokenGateProps {
  slug: string
  onSubmit: (token: string) => void
  onBackToLogin?: () => void
}

export function TokenGate({ slug, onSubmit, onBackToLogin }: TokenGateProps) {
  const [value, setValue] = useState('')

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const trimmed = value.trim()
    if (trimmed) onSubmit(trimmed)
  }

  return (
    <div className="centered-page">
      <form className="token-card" onSubmit={handleSubmit}>
        <h1>Store portal</h1>
        <p className="muted">Enter the access token for &ldquo;{slug}&rdquo; to continue.</p>
        <input
          type="password"
          autoFocus
          placeholder="Portal access token"
          value={value}
          onChange={(event) => setValue(event.target.value)}
        />
        <button type="submit" className="btn btn-primary" disabled={!value.trim()}>
          Continue
        </button>
        {onBackToLogin && (
          <button type="button" className="btn-link" onClick={onBackToLogin}>
            Back to email + password login
          </button>
        )}
      </form>
    </div>
  )
}
