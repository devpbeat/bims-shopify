import { useState } from 'react'
import type { FormEvent } from 'react'
import { useT } from '../i18n'

interface OperatorGateProps {
  slug: string
  error?: string | null
  onSubmit: (adminToken: string) => void
}

export function OperatorGate({ slug, error, onSubmit }: OperatorGateProps) {
  const { t } = useT()
  const [value, setValue] = useState('')

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const trimmed = value.trim()
    if (trimmed) onSubmit(trimmed)
  }

  return (
    <div className="centered-page">
      <form className="token-card" onSubmit={handleSubmit}>
        <h1>{t.operator.gateTitle}</h1>
        <p className="muted">{t.operator.gatePrompt(slug)}</p>
        {error && <p className="error-text">{error}</p>}
        <input
          type="password"
          autoFocus
          placeholder={t.operator.tokenPlaceholder}
          value={value}
          onChange={(event) => setValue(event.target.value)}
        />
        <button type="submit" className="btn btn-primary" disabled={!value.trim()}>
          {t.operator.enter}
        </button>
      </form>
    </div>
  )
}
