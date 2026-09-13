import { useCallback, useEffect, useRef, useState } from 'react'
import { ArrowDownLeft, Check, RefreshCw } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { useLedgerCommand } from '@/lib/command'
import { ApiError, api, money } from '@/lib/api'
import { dateLabel } from '@/lib/format'
import { Money } from '@/components/Money'
import {
  CLASSIFICATIONS, compileInboundRule, loadInboundRules,
  type IncomeCandidate, type InboundRulesPage,
} from './api'
import {
  type IncomeCandidatesPageOut, type ReportedIncomeCandidateOut, type ReportedVariationOut,
  type ReportingPolicySummary,
  loadReportedCandidates, loadReportingPolicy, nativeCandidates,
} from './reportedApi'

type StaleBanner = 'none' | 'same-scope' | 'refresh-required'

/**
 * Cash-flow usability M5 / plan M4 step 15 — income candidate workflow.
 * Recurring inbound payers detected from imported history are Likely, never
 * Confirmed. Confirming a candidate classifies the selected receipts in one
 * atomic batch and can optionally save a reusable inbound rule for future
 * imports. Nothing here classifies a receipt automatically or promotes a
 * candidate to confirmed income merely because its receipts can be valued.
 *
 * Every reported figure below is a read-only equivalent of the same native
 * receipt amount shown beside it; the classification form and inbound-rule
 * editor always submit the native anchor/amount/currency, unchanged.
 */
export function InflowsView({ generation, freshness, onSaved }: {
  generation: string; freshness: string
  onSaved: (status: string) => void | Promise<void>
}) {
  const [page, setPage] = useState<IncomeCandidatesPageOut | null>(null)
  const [rules, setRules] = useState<InboundRulesPage | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [staleBanner, setStaleBanner] = useState<StaleBanner>('none')
  const [policy, setPolicy] = useState<ReportingPolicySummary | null>(null)
  const lastGood = useRef<{ policyCurrency: string; response: IncomeCandidatesPageOut } | null>(null)
  const seq = useRef(0)

  const refreshPolicy = useCallback(() => { loadReportingPolicy().then(setPolicy).catch(() => {}) }, [])
  useEffect(() => { refreshPolicy() }, [refreshPolicy])

  const load = useCallback(() => {
    const id = ++seq.current
    setLoading(true); setError('')
    Promise.all([loadReportedCandidates(), loadInboundRules()])
      .then(([response, r]) => {
        if (id !== seq.current) return
        lastGood.current = { policyCurrency: response.reporting_currency, response }
        setPage(response); setRules(r); setStaleBanner('none')
      })
      .catch(async caught => {
        if (id !== seq.current) return
        if (caught instanceof ApiError && caught.status === 409) {
          // The queue has exactly one scope (no history/record filter), so
          // any saved response is automatically "the same scope" -- only a
          // reporting-policy change (checked below) invalidates it.
          const saved = lastGood.current
          if (saved) {
            try {
              const current = await loadReportingPolicy()
              if (id !== seq.current) return
              setPolicy(current)
              if (current.currency === saved.policyCurrency) {
                setPage(saved.response); setStaleBanner('same-scope'); return
              }
            } catch { /* fall through to refresh-required below */ }
          }
          lastGood.current = null
          setPage(null); setStaleBanner('refresh-required')
          return
        }
        setError((caught as Error).message); setPage(null); setStaleBanner('none')
      })
      .finally(() => { if (id === seq.current) setLoading(false) })
  }, [])
  useEffect(() => { load() }, [load])

  const afterSave = async (status: string) => { await onSaved(status); load() }
  const disabled = freshness === 'stale'

  if (loading && !page && staleBanner === 'none' && !error) {
    return <div className="skeleton-page" aria-label="Loading income candidates"><div /><div /><div /></div>
  }

  const reportingCurrency = page?.reporting_currency ?? policy?.currency ?? 'SGD'
  const native = page ? nativeCandidates(page) : null
  const reportedById = page ? Object.fromEntries(page.candidates.map(c => [c.candidate, c])) : {}

  return (
    <div className="inflows-view" aria-busy={loading}>
      <div className="explanation"><ArrowDownLeft size={18} /><p>Recurring inbound payers grouped from imported receipts. Each is <strong>Likely</strong>, never confirmed — past receipts do not evidence a future one. Confirming records the selected history as one decision; unrelated credits stay pending.</p></div>
      <div className="scope-caption">
        <span>Reporting in {reportingCurrency}{native ? ` · ${native.candidates.length} candidate${native.candidates.length === 1 ? '' : 's'} · ${native.exclusions.excluded_source_count} source${native.exclusions.excluded_source_count === 1 ? '' : 's'} excluded from grouping` : ''}</span>
        <Button variant="ghost" size="sm" onClick={load} disabled={loading}><RefreshCw size={14} className={loading ? 'spin' : ''} /> Refresh</Button>
      </div>
      {disabled && <div className="alert" role="status">New ledger inputs are available. Refresh before confirming income.</div>}

      {error && (
        <div className="alert error" role="alert">
          <div><strong>We couldn’t load income candidates.</strong><p>{error}</p></div>
          <Button variant="outline" onClick={load}>Try again</Button>
        </div>
      )}
      {staleBanner === 'refresh-required' && (
        <div className="alert" role="alert">
          <div><strong>Refresh required.</strong><p>The ledger has changed since the last refresh (new evidence, a rate import, or a reporting-policy change). Refresh before reading this queue.</p></div>
          <Button variant="outline" onClick={load}>Refresh</Button>
        </div>
      )}
      {staleBanner === 'same-scope' && page && (
        <div className="alert" role="status">
          <div><strong>Showing the last loaded values for this exact scope.</strong><p>New ledger inputs are available. Refresh to recompute.</p></div>
          <Button variant="outline" onClick={load}>Refresh</Button>
        </div>
      )}

      {native && <>
        {native.candidates.length === 0 && (
          <Card><div className="empty"><ArrowDownLeft size={22} /><h3>No recurring inbound payers yet</h3><p>Import at least three similar credits from one payer, 6–8 or 25–35 days apart, to see a candidate here.</p></div></Card>
        )}
        {native.candidates.map(candidate => (
          <CandidateCard
            key={candidate.candidate} candidate={candidate} reported={reportedById[candidate.candidate]}
            currency={reportingCurrency} generation={generation} disabled={disabled} onSaved={afterSave}
          />
        ))}

        <p className="muted-note">Excluded before grouping: {native.exclusions.rule}.</p>

        {rules && rules.rules.length > 0 && (
          <Card className="rules-list">
            <div className="panel-title"><div><p className="eyebrow">REUSABLE INBOUND RULES</p><h2>{rules.rules.length} active rule{rules.rules.length === 1 ? '' : 's'}</h2></div></div>
            {rules.rules.map(rule => <InboundRuleRow key={rule.rule_id} rule={rule} generation={generation} disabled={disabled} onSaved={afterSave} />)}
          </Card>
        )}
      </>}
    </div>
  )
}

function sumAmounts(values: string[]): string {
  const cents = values.reduce((total, value) => {
    const [whole, fraction = ''] = value.replace(/^[+-]/, '').split('.')
    return total + BigInt(whole + fraction.padEnd(2, '0').slice(0, 2))
  }, 0n)
  return `${cents / 100n}.${(cents % 100n).toString().padStart(2, '0')}`
}

function VariationSummary({ variation, currency }: { variation: ReportedVariationOut; currency: string }) {
  if (!variation.mean) {
    return <span className="form-error">No reported amount is available for this payer's receipts.</span>
  }
  const min = variation.minimum?.reported
  const max = variation.maximum?.reported
  return <>
    median {money(variation.mean.value, currency)}
    {min && max && <> · {money(min.value, currency)}–{money(max.value, currency)}</>}
    {variation.priced_count < variation.total_count && (
      <small className="form-error"> · {variation.total_count - variation.priced_count} of {variation.total_count} receipts could not be priced</small>
    )}
  </>
}

function CandidateCard({ candidate, reported, currency, generation, disabled, onSaved }: {
  candidate: IncomeCandidate; reported: ReportedIncomeCandidateOut | undefined; currency: string
  generation: string; disabled: boolean; onSaved: (status: string) => Promise<void>
}) {
  const [selected, setSelected] = useState<Set<string>>(() => new Set(candidate.occurrences.map(o => o.anchor)))
  const [classification, setClassification] = useState('income')
  const [reason, setReason] = useState('')
  const [saveRule, setSaveRule] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const window = candidate.timing_window
  const chosen = candidate.occurrences.filter(o => selected.has(o.anchor))
  const reportedByAnchor = Object.fromEntries((reported?.occurrences ?? []).map(o => [o.anchor, o]))

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
      // Native anchors and native classification only -- no reported value
      // is ever computed or copied into a command.
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
        <div><dt>Amount</dt><dd>{reported ? <VariationSummary variation={reported.variation} currency={currency} /> : `native ${money(candidate.amount_variation.mean_amount, candidate.currency)}`}{' '}<small>native {money(candidate.amount_variation.min_amount, candidate.currency)}–{money(candidate.amount_variation.max_amount, candidate.currency)} ({candidate.amount_variation.basis})</small></dd></div>
        <div><dt>Observed</dt><dd>{dateLabel(candidate.first_observed_on)} → {dateLabel(candidate.last_receipt_on)} · {candidate.source_count} receipts</dd></div>
        <div><dt>Next expected</dt><dd>{dateLabel(window.next_expected_between[0])} – {dateLabel(window.next_expected_between[1])}{window.day_of_month_min ? ` · day ${window.day_of_month_min}–${window.day_of_month_max}` : ''}</dd></div>
      </dl>

      <table className="income-occurrences">
        <thead><tr><th className="pick-cell"><span className="sr-only">Select</span></th><th>Date</th><th className="amount">Native amount</th><th className="amount">Reported ({currency})</th></tr></thead>
        <tbody>
          {candidate.occurrences.map(o => {
            const reportedOccurrence = reportedByAnchor[o.anchor]
            return (
              <tr key={o.anchor}>
                <td className="pick-cell"><input type="checkbox" aria-label={`Include ${o.date}`} checked={selected.has(o.anchor)} onChange={() => toggle(o.anchor)} /></td>
                <td>{dateLabel(o.date)}</td>
                <td className="amount">{money(o.amount, candidate.currency)}</td>
                <td className="amount">{reportedOccurrence?.reported ? <Money value={reportedOccurrence.reported} /> : <span className="muted-note">No evidence</span>}</td>
              </tr>
            )
          })}
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
        <span className="muted-note">{chosen.length ? `${candidate.currency} ${sumAmounts(chosen.map(o => o.amount))} (native)` : 'Select at least one receipt'}</span>
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
