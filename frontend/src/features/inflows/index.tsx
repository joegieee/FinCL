import { useCallback, useEffect, useRef, useState } from 'react'
import { ArrowDownLeft, Check, RefreshCw } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { AsyncView } from '@/components/AsyncView'
import { useLedgerCommand } from '@/lib/command'
import { api, money } from '@/lib/api'
import { dateLabel } from '@/lib/format'
import {
  CLASSIFICATIONS, compileInboundRule, loadCandidates, loadInboundRules,
  type CandidatesPage, type IncomeCandidate, type InboundRulesPage,
} from './api'

/**
 * Cash-flow usability M5 — income candidate workflow. Recurring inbound payers
 * detected from imported history are Likely, never Confirmed. Confirming a
 * candidate classifies the selected receipts in one atomic batch and can
 * optionally save a reusable inbound rule for future imports. Nothing here
 * classifies a receipt automatically.
 */
export function InflowsView({ generation, freshness, onSaved }: {
  generation: string; freshness: string
  onSaved: (status: string) => void | Promise<void>
}) {
  const [page, setPage] = useState<CandidatesPage | null>(null)
  const [rules, setRules] = useState<InboundRulesPage | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const seq = useRef(0)

  const load = useCallback(() => {
    const id = ++seq.current
    setLoading(true); setError('')
    Promise.all([loadCandidates(), loadInboundRules()])
      .then(([c, r]) => { if (id === seq.current) { setPage(c); setRules(r) } })
      .catch(e => { if (id === seq.current) setError((e as Error).message) })
      .finally(() => { if (id === seq.current) setLoading(false) })
  }, [])
  useEffect(() => { load() }, [load])

  const afterSave = async (status: string) => { await onSaved(status); load() }
  const disabled = freshness === 'stale'

  return (
    <AsyncView loading={loading} error={error} data={page} onRetry={load} loadingLabel="Loading income candidates" errorTitle="We couldn’t load income candidates.">
      {page => (
        <div className="inflows-view">
          <div className="explanation"><ArrowDownLeft size={18} /><p>Recurring inbound payers grouped from imported receipts. Each is <strong>Likely</strong>, never confirmed — past receipts do not evidence a future one. Confirming records the selected history as one decision; unrelated credits stay pending.</p></div>
          <div className="scope-caption">
            <span>{page.candidates.length} candidate{page.candidates.length === 1 ? '' : 's'} · {page.exclusions.excluded_source_count} source{page.exclusions.excluded_source_count === 1 ? '' : 's'} excluded from grouping</span>
            <Button variant="ghost" size="sm" onClick={load} disabled={loading}><RefreshCw size={14} className={loading ? 'spin' : ''} /> Refresh</Button>
          </div>
          {disabled && <div className="alert" role="status">New ledger inputs are available. Refresh before confirming income.</div>}

          {page.candidates.length === 0 && (
            <Card><div className="empty"><ArrowDownLeft size={22} /><h3>No recurring inbound payers yet</h3><p>Import at least three similar credits from one payer, 6–8 or 25–35 days apart, to see a candidate here.</p></div></Card>
          )}
          {page.candidates.map(candidate => (
            <CandidateCard key={candidate.candidate} candidate={candidate} generation={generation} disabled={disabled} onSaved={afterSave} />
          ))}

          <p className="muted-note">Excluded before grouping: {page.exclusions.rule}.</p>

          {rules && rules.rules.length > 0 && (
            <Card className="rules-list">
              <div className="panel-title"><div><p className="eyebrow">REUSABLE INBOUND RULES</p><h2>{rules.rules.length} active rule{rules.rules.length === 1 ? '' : 's'}</h2></div></div>
              {rules.rules.map(rule => <InboundRuleRow key={rule.rule_id} rule={rule} generation={generation} disabled={disabled} onSaved={afterSave} />)}
            </Card>
          )}
        </div>
      )}
    </AsyncView>
  )
}

function sumAmounts(values: string[]): string {
  const cents = values.reduce((total, value) => {
    const [whole, fraction = ''] = value.replace(/^[+-]/, '').split('.')
    return total + BigInt(whole + fraction.padEnd(2, '0').slice(0, 2))
  }, 0n)
  return `${cents / 100n}.${(cents % 100n).toString().padStart(2, '0')}`
}

function CandidateCard({ candidate, generation, disabled, onSaved }: {
  candidate: IncomeCandidate; generation: string; disabled: boolean; onSaved: (status: string) => Promise<void>
}) {
  const [selected, setSelected] = useState<Set<string>>(() => new Set(candidate.occurrences.map(o => o.anchor)))
  const [classification, setClassification] = useState('income')
  const [reason, setReason] = useState('')
  const [saveRule, setSaveRule] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const variation = candidate.amount_variation
  const window = candidate.timing_window
  const chosen = candidate.occurrences.filter(o => selected.has(o.anchor))

  function toggle(anchor: string) {
    setSelected(current => {
      const next = new Set(current)
      if (next.has(anchor)) next.delete(anchor); else next.add(anchor)
      return next
    })
  }

  async function confirm() {
    if (!chosen.length || !reason.trim()) return
    setBusy(true); setError('')
    try {
      const items: Array<{ kind: string; anchors: Record<string, string>; payload: Record<string, unknown> }> =
        chosen.map(o => ({ kind: 'INBOUND_CLASSIFICATION', anchors: { entry: o.anchor }, payload: { classification, reason: reason.trim() } }))
      if (saveRule) {
        const compiled = await compileInboundRule(generation, { description_contains: candidate.merchant_key }, classification, reason.trim())
        items.push(compiled.command)
      }
      const result = await api<{ status: string }>('batch', {
        request_id: crypto.randomUUID(), expected_generation: generation,
        reason: reason.trim(), items,
      })
      await onSaved(result.status)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card className="income-candidate">
      <div className="panel-title">
        <div>
          <p className="eyebrow">{candidate.cadence.toUpperCase()} · {candidate.currency}</p>
          <h2>{candidate.merchant_key}</h2>
        </div>
        <Badge variant="outline">Likely</Badge>
      </div>
      <p className="muted-note">{candidate.review_question} {candidate.confidence_reason}</p>
      <dl className="detail-list">
        <div><dt>Amount</dt><dd>median {money(variation.mean_amount, candidate.currency)} · {money(variation.min_amount, candidate.currency)}–{money(variation.max_amount, candidate.currency)} ({variation.basis})</dd></div>
        <div><dt>Observed</dt><dd>{dateLabel(candidate.first_observed_on)} → {dateLabel(candidate.last_receipt_on)} · {candidate.source_count} receipts</dd></div>
        <div><dt>Next expected</dt><dd>{dateLabel(window.next_expected_between[0])} – {dateLabel(window.next_expected_between[1])}{window.day_of_month_min ? ` · day ${window.day_of_month_min}–${window.day_of_month_max}` : ''}</dd></div>
      </dl>

      <table className="income-occurrences">
        <thead><tr><th className="pick-cell"><span className="sr-only">Select</span></th><th>Date</th><th className="amount">Amount</th></tr></thead>
        <tbody>
          {candidate.occurrences.map(o => (
            <tr key={o.anchor}>
              <td className="pick-cell"><input type="checkbox" aria-label={`Include ${o.date}`} checked={selected.has(o.anchor)} onChange={() => toggle(o.anchor)} /></td>
              <td>{dateLabel(o.date)}</td>
              <td className="amount">{money(o.amount, candidate.currency)}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="bulk-form">
        <label>Classify as
          <select aria-label="Classification" value={classification} onChange={e => setClassification(e.target.value)}>
            {CLASSIFICATIONS.map(c => <option key={c} value={c}>{c.replaceAll('_', ' ')}</option>)}
          </select>
        </label>
        <label>Reason<Input aria-label="Reason" required maxLength={2000} value={reason} onChange={e => setReason(e.target.value)} placeholder="Why is this the right classification?" /></label>
      </div>
      <label className="inline-check"><input type="checkbox" checked={saveRule} onChange={e => setSaveRule(e.target.checked)} /> Also save a reusable rule for future credits whose description contains “{candidate.merchant_key}”</label>

      {error && <p className="form-error" role="alert">{error}</p>}
      {disabled && <p className="form-error">Refresh the ledger before confirming income.</p>}
      <div className="dialog-actions">
        <Button type="button" disabled={disabled || busy || !chosen.length || !reason.trim()} onClick={() => void confirm()}>
          {busy ? 'Saving…' : `Confirm ${chosen.length} receipt${chosen.length === 1 ? '' : 's'}${saveRule ? ' + rule' : ''}`}
        </Button>
        <span className="muted-note">{chosen.length ? `${candidate.currency} ${sumAmounts(chosen.map(o => o.amount))}` : 'Select at least one receipt'}</span>
      </div>
    </Card>
  )
}

function InboundRuleRow({ rule, generation, disabled, onSaved }: {
  rule: InboundRulesPage['rules'][number]; generation: string; disabled: boolean; onSaved: (status: string) => void | Promise<void>
}) {
  const retire = useLedgerCommand(generation, onSaved)
  return (
    <div className="rule-row">
      <div>
        <strong>{ruleSentence(rule.match)} → {rule.classification.replaceAll('_', ' ')}</strong>
        <p>Suggests this classification for a future inflow that has no decision of its own.</p>
        {rule.reason && <small>{rule.reason}</small>}
      </div>
      <Button variant="ghost" size="sm" disabled={disabled || retire.busy}
        onClick={() => void retire.act({ kind: 'INBOUND_RULE_RETIRE', anchors: { rule_id: rule.rule_id }, payload: { reason: `Retire inbound rule ${rule.rule_id}` } })}>
        {retire.previewed && !retire.attempted ? 'Confirm retire' : retire.busy ? 'Working…' : 'Retire'}
      </Button>
      {retire.error && <p className="form-error" role="alert">{retire.error}</p>}
      {retire.previewed && !retire.attempted && <p className="muted-note"><Check size={13} /> Retire removes the rule; classified receipts keep their decision.</p>}
    </div>
  )
}

function ruleSentence(match: Record<string, unknown>) {
  if ('merchant_canonical' in match) return `Merchant is “${match.merchant_canonical}”`
  if ('paynow_to' in match) return `PayNow source is “${match.paynow_to}”`
  if ('description_prefix' in match) return `Description starts with “${match.description_prefix}”`
  return `Description contains “${match.description_contains}”`
}
