import { useState } from 'react'
import type { FormEvent } from 'react'
import { useT } from '../i18n'
import { LanguageToggle } from './LanguageToggle'

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
    <div className="centered-page">
      <form className="token-card" onSubmit={handleSubmit}>
        <div className="token-card-top">
          <h1>{t.login.title}</h1>
          <LanguageToggle />
        </div>
        <p className="muted">{t.login.accessKeyPrompt(slug)}</p>
        <input
          type="password"
          autoFocus
          placeholder={t.login.accessKeyPlaceholder}
          value={value}
          onChange={(event) => setValue(event.target.value)}
        />
        <button type="submit" className="btn btn-primary" disabled={!value.trim()}>
          {t.common.continueLabel}
        </button>
        {onBackToLogin && (
          <button type="button" className="btn-link" onClick={onBackToLogin}>
            {t.common.backToLogin}
          </button>
        )}
      </form>
    </div>
  )
}
