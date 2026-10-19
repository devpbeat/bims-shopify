import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { getPaymentsConfig, putPaymentsConfig, testPaymentsConnection } from '../api/client'
import type { PaymentsTestResult } from '../api/types'
import { useT } from '../i18n'

interface PaymentsTabProps {
  slug: string
  token: string
}

export function PaymentsTab({ slug, token }: PaymentsTabProps) {
  const { t } = useT()
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [editable, setEditable] = useState(true)
  const [privateKeyConfigured, setPrivateKeyConfigured] = useState(false)
  const [confirmUrl, setConfirmUrl] = useState('')
  const [publicKey, setPublicKey] = useState('')
  const [environment, setEnvironment] = useState('staging')
  const [gatewayName, setGatewayName] = useState('')
  // Write-only: never populated from the API, only sent when the operator types a new value.
  const [privateKey, setPrivateKey] = useState('')
  const [saving, setSaving] = useState(false)
  const [saveMessage, setSaveMessage] = useState<{ ok: boolean; text: string } | null>(null)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<PaymentsTestResult | null>(null)
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setLoadError(null)

    getPaymentsConfig(slug, token)
      .then((config) => {
        if (cancelled) return
        setEditable(config.editable)
        setPrivateKeyConfigured(config.private_key_configured)
        setConfirmUrl(config.confirm_url)
        setPublicKey(config.public_key ?? '')
        setEnvironment(config.environment === 'production' ? 'production' : 'staging')
        setGatewayName(config.gateway_name ?? '')
      })
      .catch((err: unknown) => {
        if (!cancelled) setLoadError(err instanceof Error ? err.message : t.payments.loadFailed)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [slug, token, t.payments.loadFailed])

  async function handleSave(event: FormEvent) {
    event.preventDefault()
    if (!editable) return
    setSaving(true)
    setSaveMessage(null)
    setTestResult(null)
    try {
      const config = await putPaymentsConfig(slug, token, {
        public_key: publicKey,
        environment,
        gateway_name: gatewayName,
        ...(privateKey.trim() ? { private_key: privateKey } : {}),
      })
      setPrivateKeyConfigured(config.private_key_configured)
      setPrivateKey('')
      setSaveMessage({ ok: true, text: t.payments.saved })
    } catch (err) {
      setSaveMessage({ ok: false, text: err instanceof Error ? err.message : t.payments.saveFailed })
    } finally {
      setSaving(false)
    }
  }

  async function handleTest() {
    setTesting(true)
    setTestResult(null)
    try {
      setTestResult(await testPaymentsConnection(slug, token))
    } catch (err) {
      setTestResult({ ok: false, reason: 'unexpected', message: err instanceof Error ? err.message : t.payments.testFailed })
    } finally {
      setTesting(false)
    }
  }

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(confirmUrl)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 2000)
    } catch {
      // Clipboard access can be denied; the URL stays selectable in the read-only input.
    }
  }

  if (loading) return <p className="muted">{t.payments.loading}</p>
  if (loadError) return <p className="error-text">{loadError}</p>

  return (
    <form className="ops-tab" onSubmit={handleSave}>
      <section className="ops-card">
        <h3>{t.payments.title}</h3>
        <p className="muted">{t.payments.intro}</p>
        {!editable && <p className="muted">{t.payments.readOnlyNotice}</p>}

        <label className="ops-field">
          {t.payments.publicKeyLabel}
          <input type="text" value={publicKey} disabled={!editable} onChange={(e) => setPublicKey(e.target.value)} />
        </label>

        <label className="ops-field">
          <span>
            {t.payments.privateKeyLabel}{' '}
            <strong>
              ({privateKeyConfigured ? t.payments.privateKeyConfiguredBadge : t.payments.privateKeyNotConfiguredBadge})
            </strong>
          </span>
          <input
            type="password"
            value={privateKey}
            autoComplete="new-password"
            placeholder={t.payments.privateKeyPlaceholder}
            disabled={!editable}
            onChange={(e) => setPrivateKey(e.target.value)}
          />
        </label>
        <p className="muted">{t.payments.privateKeyHelp}</p>

        <label className="ops-field">
          {t.payments.environmentLabel}
          <select value={environment} disabled={!editable} onChange={(e) => setEnvironment(e.target.value)}>
            <option value="staging">{t.payments.environmentStaging}</option>
            <option value="production">{t.payments.environmentProduction}</option>
          </select>
        </label>

        <label className="ops-field">
          {t.payments.gatewayNameLabel}
          <input type="text" value={gatewayName} disabled={!editable} onChange={(e) => setGatewayName(e.target.value)} />
        </label>
        <p className="muted">{t.payments.gatewayNameHelp}</p>

        {editable && (
          <div>
            <button type="submit" className="btn btn-primary" disabled={saving}>
              {saving ? t.payments.saving : t.payments.save}
            </button>
          </div>
        )}
        {saveMessage && <p className={saveMessage.ok ? 'muted' : 'error-text'}>{saveMessage.text}</p>}
      </section>

      <section className="ops-card">
        <h3>{t.payments.confirmUrlLabel}</h3>
        <p className="muted">{t.payments.confirmUrlHelp}</p>
        <div>
          <input type="text" readOnly value={confirmUrl} size={60} onFocus={(e) => e.currentTarget.select()} />{' '}
          <button type="button" className="btn btn-secondary" onClick={handleCopy}>
            {copied ? t.payments.copied : t.payments.copy}
          </button>
        </div>
      </section>

      {editable && (
        <section className="ops-card">
          <h3>{t.payments.testConnection}</h3>
          <p className="muted">{t.payments.testHelp}</p>
          <div>
            <button type="button" className="btn btn-secondary" disabled={testing} onClick={handleTest}>
              {testing ? t.payments.testing : t.payments.testConnection}
            </button>
          </div>
          {testResult && (
            <p className={testResult.ok ? 'muted' : 'error-text'}>
              {testResult.ok ? t.payments.testOk : t.payments.testFailed} {testResult.message}
            </p>
          )}
        </section>
      )}
    </form>
  )
}
