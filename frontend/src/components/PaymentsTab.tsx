import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { getPaymentsConfig, putPaymentsConfig, testPaymentsConnection } from '../api/client'
import type { PaymentsTestResult } from '../api/types'
import { useT } from '../i18n'
import { Field } from './Field'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'

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

  if (loading) {
    return (
      <div className="flex flex-col gap-3" role="status" aria-live="polite">
        <p className="text-sm text-muted-foreground">{t.payments.loading}</p>
        <Skeleton className="h-40 w-full" />
        <Skeleton className="h-24 w-full" />
      </div>
    )
  }
  if (loadError) {
    return (
      <Alert variant="destructive">
        <AlertDescription>{loadError}</AlertDescription>
      </Alert>
    )
  }

  const touch = 'h-11 sm:h-9'

  return (
    <div className="flex flex-col gap-4">
      <form onSubmit={handleSave}>
        <Card size="sm">
          <CardHeader>
            <CardTitle>{t.payments.title}</CardTitle>
            <CardDescription>{t.payments.intro}</CardDescription>
          </CardHeader>
          <CardContent className="gap-4">
            {!editable && (
              <Alert>
                <AlertDescription>{t.payments.readOnlyNotice}</AlertDescription>
              </Alert>
            )}

            <Field label={t.payments.publicKeyLabel}>
              {(control) => (
                <Input {...control} className={touch} value={publicKey} disabled={!editable} onChange={(e) => setPublicKey(e.target.value)} />
              )}
            </Field>

            <Field
              hint={t.payments.privateKeyHelp}
              label={
                <>
                  {t.payments.privateKeyLabel}
                  <Badge variant={privateKeyConfigured ? 'secondary' : 'outline'}>
                    {privateKeyConfigured ? t.payments.privateKeyConfiguredBadge : t.payments.privateKeyNotConfiguredBadge}
                  </Badge>
                </>
              }
            >
              {(control) => (
                <Input
                  {...control}
                  className={touch}
                  type="password"
                  value={privateKey}
                  autoComplete="new-password"
                  placeholder={t.payments.privateKeyPlaceholder}
                  disabled={!editable}
                  onChange={(e) => setPrivateKey(e.target.value)}
                />
              )}
            </Field>

            <Field label={t.payments.environmentLabel}>
              {(control) => (
                <Select value={environment} disabled={!editable} onValueChange={setEnvironment}>
                  <SelectTrigger {...control} className={`${touch} w-full sm:w-64`}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="staging">{t.payments.environmentStaging}</SelectItem>
                    <SelectItem value="production">{t.payments.environmentProduction}</SelectItem>
                  </SelectContent>
                </Select>
              )}
            </Field>

            <Field label={t.payments.gatewayNameLabel} hint={t.payments.gatewayNameHelp}>
              {(control) => (
                <Input {...control} className={touch} value={gatewayName} disabled={!editable} onChange={(e) => setGatewayName(e.target.value)} />
              )}
            </Field>

            {editable && (
              <Button type="submit" className={`${touch} w-full sm:w-auto sm:self-start`} disabled={saving}>
                {saving ? t.payments.saving : t.payments.save}
              </Button>
            )}
            {saveMessage && (
              <Alert variant={saveMessage.ok ? 'default' : 'destructive'} role={saveMessage.ok ? 'status' : 'alert'}>
                <AlertDescription>{saveMessage.text}</AlertDescription>
              </Alert>
            )}
          </CardContent>
        </Card>
      </form>

      <Card size="sm">
        <CardHeader>
          <CardTitle>{t.payments.confirmUrlLabel}</CardTitle>
          <CardDescription>{t.payments.confirmUrlHelp}</CardDescription>
        </CardHeader>
        <CardContent className="gap-2 sm:flex-row">
          <Input
            aria-label={t.payments.confirmUrlLabel}
            className={`${touch} min-w-0 font-mono text-xs`}
            readOnly
            value={confirmUrl}
            onFocus={(e) => e.currentTarget.select()}
          />
          <Button type="button" variant="outline" className={`${touch} shrink-0`} onClick={handleCopy} aria-live="polite">
            {copied ? t.payments.copied : t.payments.copy}
          </Button>
        </CardContent>
      </Card>

      {editable && (
        <Card size="sm">
          <CardHeader>
            <CardTitle>{t.payments.testConnection}</CardTitle>
            <CardDescription>{t.payments.testHelp}</CardDescription>
          </CardHeader>
          <CardContent className="gap-3">
            <Button type="button" variant="outline" className={`${touch} w-full sm:w-auto sm:self-start`} disabled={testing} onClick={handleTest}>
              {testing ? t.payments.testing : t.payments.testConnection}
            </Button>
            {testResult && (
              <Alert variant={testResult.ok ? 'default' : 'destructive'} role={testResult.ok ? 'status' : 'alert'}>
                <AlertDescription>
                  {testResult.ok ? t.payments.testOk : t.payments.testFailed} {testResult.message}
                </AlertDescription>
              </Alert>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  )
}
