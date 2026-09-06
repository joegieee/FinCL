import { useCallback, useEffect, useRef, useState } from 'react'
import { ArrowRight, Check, Layers2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { api, ApiError, money, type components } from '@/lib/api'

type Review = components['schemas']['RelationshipsReview']
type Proposal = components['schemas']['ReviewProposal']

/**
 * Manual duplicate review (v0.2 slice 3). Each proposal is two entries that
 * match on account, amount, date and merchant across distinct imports.
 * "Same transaction" suppresses the candidate from every total; "Distinct
 * purchases" records the rejection. Similarity alone never suppresses a row.
 */
export function RelationshipEditor({
  generation, freshness, onSaved,
}: {
  generation: string
  freshness: string
  onSaved: (status: string) => Promise<void> | void
}) {
  const [review, setReview] = useState<Review | null>(null)
  const [error, setError] = useState('')
  const seq = useRef(0)

  const load = useCallback(() => {
    const id = ++seq.current
    setError('')
    api<Review>(`relationships/review?generation=${encodeURIComponent(generation)}`)
      .then(r => { if (id === seq.current) setReview(r) })
      .catch(e => { if (id === seq.current) setError((e as Error).message) })
  }, [generation])

  useEffect(() => { load() }, [load])

  return (
    <Card className="recurring-card">
      <div className="panel-title">
        <div>
          <p className="eyebrow">POSSIBLE DUPLICATES</p>
          <h2>{review ? `${review.total} pair${review.total === 1 ? '' : 's'} across imports` : 'Loading…'}</h2>
        </div>
      </div>
      {error && <div className="alert error" role="alert"><p>{error}</p></div>}
      {review?.proposals.length === 0 && (
        <div className="empty">
          <Layers2 size={22} />
          <h3>No duplicate proposals</h3>
          <p>These appear only when the same transaction is present in two different imported files.</p>
        </div>
      )}
      {review?.proposals.map(p => (
        <DuplicateRow
          key={`${p.primary}${p.candidate}`}
          proposal={p}
          generation={generation}
          disabled={freshness === 'stale'}
          onSaved={async status => { await onSaved(status); load() }}
        />
      ))}
    </Card>
  )
}

function DuplicateRow({
  proposal: p, generation, disabled, onSaved,
}: {
  proposal: Proposal
  generation: string
  disabled: boolean
  onSaved: (status: string) => Promise<void> | void
}) {
  const [decision, setDecision] = useState<'confirm' | 'reject'>('confirm')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const inFlight = useRef(false)

  async function submit() {
    if (inFlight.current || !reason.trim()) return
    inFlight.current = true; setBusy(true); setError('')
    const kind = decision === 'confirm' ? 'DUPLICATE_CONFIRM' : 'DUPLICATE_REJECT'
    const body = {
      request_id: crypto.randomUUID(),
      expected_generation: generation,
      kind,
      anchors: { primary: p.primary, candidate: p.candidate },
      payload: { reason: reason.trim() },
    }
    try {
      await api('preview', body)
      const result = await api<{ status: string }>('commands', body)
      await onSaved(result.status)
    } catch (e) {
      setError((e as Error).message)
      if (e instanceof ApiError && [409, 422].includes(e.status)) { /* reload upstream */ }
    } finally {
      inFlight.current = false; setBusy(false)
    }
  }

  return (
    <div className="proposal">
      <div className="proposal-legs">
        <div>
          <span className="eyebrow">PRIMARY (KEPT)</span>
          <strong>{p.merchant}</strong>
          <small>{p.account} · {p.date} · {money(p.amount, p.currency)}</small>
          <small className="anchor">{p.primary}</small>
        </div>
        <div>
          <span className="eyebrow">CANDIDATE</span>
          <strong>{p.merchant}</strong>
          <small>{p.account} · {p.date} · {money(p.amount, p.currency)}</small>
          <small className="anchor">{p.candidate}</small>
        </div>
      </div>
      <p className="muted-note">{p.basis}. Confirming suppresses the candidate from every total.</p>
      <div className="proposal-decide">
        <select aria-label="Duplicate decision" value={decision} onChange={e => setDecision(e.target.value as 'confirm' | 'reject')} disabled={busy}>
          <option value="confirm">Same transaction — suppress the candidate</option>
          <option value="reject">Distinct purchases — keep both</option>
        </select>
        <Input
          required
          maxLength={2000}
          placeholder="Why is this the right call?"
          value={reason}
          onChange={e => setReason(e.target.value)}
          disabled={busy}
        />
      </div>
      {error && <p className="form-error" role="alert">{error}</p>}
      {disabled && <p className="form-error">Refresh the ledger before deciding.</p>}
      <div className="dialog-actions">
        <Button type="button" onClick={submit} disabled={busy || disabled || !reason.trim()}>
          {busy ? 'Working…' : decision === 'confirm' ? 'Confirm duplicate' : 'Record as distinct'}
          {!busy && (decision === 'confirm' ? <Check size={15} /> : <ArrowRight size={15} />)}
        </Button>
      </div>
    </div>
  )
}
