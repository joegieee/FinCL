import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { api, ApiError, type Command } from '@/lib/api'
import { loadCurrencyRegistry, type CurrencyRegistryEntry, type ReportingPolicySummary } from './reportedApi'

/**
 * The reporting-currency settings control (M3b.3 step 10, §6.3). Reuses the
 * existing `/api/v1/preview` + `/api/v1/commands` path for `REPORTING_CURRENCY_SET`
 * — the same two-step preview/commit contract every other command in this app
 * uses (see `App.tsx`'s `useCommand`) — since no other usable control for this
 * decision kind exists yet in the frontend. Saving never calls `/api/v1/refresh`
 * itself: the ledger stays exactly as stale/fresh as any other recorded decision
 * leaves it, and the caller decides when to refresh (§6.3: "must not ...
 * automatically refresh").
 */
export function ReportingPolicySettings({
  generation, policy, onChanged,
}: {
  generation: string
  policy: ReportingPolicySummary | null
  onChanged: () => void
}) {
  const [open, setOpen] = useState(false)
  const [registry, setRegistry] = useState<CurrencyRegistryEntry[]>([])
  const [currency, setCurrency] = useState('')
  const [reason, setReason] = useState('')
  const [preview, setPreview] = useState<Command | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [done, setDone] = useState(false)

  function openForm() {
    setOpen(true)
    setCurrency(policy?.currency ?? 'SGD')
    setReason('')
    setPreview(null)
    setError('')
    setDone(false)
    if (!registry.length) loadCurrencyRegistry().then(setRegistry).catch(() => {})
  }

  async function submit() {
    setBusy(true)
    setError('')
    try {
      if (!preview) {
        const command: Command = {
          kind: 'REPORTING_CURRENCY_SET', anchors: { ledger: 'default' }, payload: { currency, reason },
          request_id: crypto.randomUUID(), expected_generation: generation,
        }
        await api('preview', command)
        setPreview(command)
      } else {
        await api('commands', preview)
        setDone(true)
        onChanged()
      }
    } catch (e) {
      setError((e as Error).message)
      if (e instanceof ApiError && [409, 422].includes(e.status)) setPreview(null)
    } finally {
      setBusy(false)
    }
  }

  if (!open) {
    return (
      <p className="scope-caption">
        <span>Reporting in {policy?.currency ?? 'SGD'}{policy?.is_default && ' (ledger default)'}</span>
        <Button variant="ghost" size="sm" onClick={openForm}>Change</Button>
      </p>
    )
  }

  return (
    <form className="reporting-policy-form" onSubmit={e => { e.preventDefault(); void submit() }}>
      <label>
        Reporting currency
        <select
          aria-label="Reporting currency"
          value={currency}
          disabled={busy || done}
          onChange={e => { setCurrency(e.target.value); setPreview(null) }}
        >
          {registry.length
            ? registry.map(c => <option key={c.code} value={c.code}>{c.code} — {c.display_name}</option>)
            : <option value={currency}>{currency}</option>}
        </select>
      </label>
      <label>
        Reason
        <Input
          required maxLength={2000} value={reason} disabled={busy || done}
          placeholder="Why change the ledger's reporting currency?"
          onChange={e => { setReason(e.target.value); setPreview(null) }}
        />
      </label>
      {error && <p className="form-error" role="alert">{error}</p>}
      {done ? (
        <p role="status">Saved. This changes only the ledger's reporting-currency setting, never native records. Use Refresh before reading updated totals.</p>
      ) : (
        <div className="dialog-actions">
          <Button type="submit" disabled={busy || !currency || !reason.trim()}>
            {busy ? 'Working…' : preview ? 'Save change' : 'Preview change'}
          </Button>
          <Button type="button" variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
        </div>
      )}
    </form>
  )
}
