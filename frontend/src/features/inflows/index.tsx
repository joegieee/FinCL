import { useCallback, useEffect, useRef, useState } from 'react'
import { ArrowDownLeft, ArrowRight, Check, RefreshCw } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { useBatchCommand, useLedgerCommand } from '@/lib/command'
import { ApiError, money } from '@/lib/api'
import { dateLabel } from '@/lib/format'
import { Money } from '@/components/Money'
import { CategoryPicker, rememberCategory } from '@/features/classification/CategoryPicker'
import { ClassificationPreviewSummary } from '@/features/classification/PreviewSummary'
import {
  INCOMING_LABELS, previewClassification,
  type ClassificationDraftBody, type ClassificationPreview,
} from '@/features/classification/api'
import {
  CLASSIFICATIONS, compileInboundRule, loadInboundRules,
  type CompiledInboundRule, type IncomeCandidate, type InboundRulesPage,
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
export function InflowsView({ generation, freshness, categories, onOpenTransactions, onSaved }: {
  generation: string; freshness: string
  categories: string[]
  /** Discoverable route to eligible one-off/unresolved credits that never
   * satisfy recurring-candidate detection (§3.3, §9): opens Transactions
   * filtered to unclassified incoming money instead of requiring three
   * similar receipts before a credit can be classified here. */
  onOpenTransactions: () => void
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
      <div className="alert" role="status">
        <div><strong>Looking for a one-off receipt?</strong><p>A credit does not need three similar receipts to be classified. Isolated and unresolved incoming money can be classified directly from Transactions.</p></div>
        <Button variant="outline" onClick={onOpenTransactions}>Open in Transactions <ArrowRight size={15} /></Button>
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
            currency={reportingCurrency} categories={categories} generation={generation} disabled={disabled} onSaved={afterSave}
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
    average {money(variation.mean.value, currency)}
    {min && max && <> · {money(min.value, currency)}–{money(max.value, currency)}</>}
    {variation.priced_count < variation.total_count && (
      <small className="form-error"> · {variation.total_count - variation.priced_count} of {variation.total_count} receipts could not be priced</small>
    )}
  </>
}

function CandidateCard({ candidate, reported, currency, categories, generation, disabled, onSaved }: {
  candidate: IncomeCandidate; reported: ReportedIncomeCandidateOut | undefined; currency: string
  categories: string[]; generation: string; disabled: boolean; onSaved: (status: string) => Promise<void>
}) {
  const [selected, setSelected] = useState<Set<string>>(() => new Set(candidate.occurrences.map(o => o.anchor)))
  const [category, setCategory] = useState('')
  const [classification, setClassification] = useState('income')
  const [reason, setReason] = useState('')
  const [saveRule, setSaveRule] = useState(false)
  const [ruleCompiled, setRuleCompiled] = useState<CompiledInboundRule | null>(null)
  const [ruleError, setRuleError] = useState('')
  const [preview, setPreview] = useState<ClassificationPreview | null>(null)
  const [previewError, setPreviewError] = useState('')
  const [previewing, setPreviewing] = useState(false)
  const window = candidate.timing_window
  const chosen = candidate.occurrences.filter(o => selected.has(o.anchor))
  const anchors = chosen.map(o => o.anchor)
  const reportedByAnchor = Object.fromEntries((reported?.occurrences ?? []).map(o => [o.anchor, o]))

  // Reuses the exact combined preview and retry-safe batch lifecycle every
  // other classification entry point uses (PLAN-unified-transaction-
  // classification.md Step 4): confirming a candidate's history is a
  // multi-anchor classification draft, not a bespoke direct batch write.
  const batch = useBatchCommand(generation, async status => { await onSaved(status) })
  useEffect(() => { if (batch.error && !batch.submitted) setPreview(null) }, [batch.error, batch.submitted])

  const scopeKey = [...anchors].sort().join(',')
  useEffect(() => {
    setPreview(null); setPreviewError(''); batch.invalidate()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scopeKey])

  function toggle(anchor: string) {
    setSelected(current => {
      const next = new Set(current)
      if (next.has(anchor)) next.delete(anchor); else next.add(anchor)
      return next
    })
  }

  function invalidatePreview() { setPreview(null); setPreviewError(''); setRuleCompiled(null); setRuleError(''); batch.invalidate() }

  async function reviewChanges() {
    if (!anchors.length || !reason.trim() || previewing) return
    setPreviewing(true); setPreviewError(''); setRuleError('')
    try {
      // Every chosen receipt is explicitly (re)classified -- this is the
      // deliberate "confirm this history" decision, not a fill-missing
      // default -- while category is left at Keep unless the user picks one,
      // preserving every existing manual category untouched (§3.3, §9).
      const draft: ClassificationDraftBody = {
        generation, anchors,
        category: category ? { intent: 'set', value: category } : undefined,
        incoming_type: { intent: 'set', value: classification },
        policy: 'replace', reason: reason.trim(),
      }
      const result = await previewClassification(draft)
      setPreview(result)
      if (category) rememberCategory(category)
      // The optional rule's own match impact (PLAN-unified-transaction-
      // classification.md Step 7) is now checked and shown at the same
      // review step as the receipt classification, rather than compiled
      // silently right before commit with no chance to see it first. A rule
      // compile failure never discards the classification preview above --
      // the receipt classification and the optional rule are reviewed
      // together but stay two distinguishable outcomes.
      if (saveRule) {
        try { setRuleCompiled(await compileInboundRule(generation, { description_contains: candidate.merchant_key }, classification, reason.trim())) }
        catch (e) { setRuleError((e as Error).message) }
      }
    } catch (e) { setPreviewError((e as Error).message) }
    finally { setPreviewing(false) }
  }

  async function saveChanges() {
    if (!preview) return
    const items = ruleCompiled ? [...preview.commands, ruleCompiled.command] : preview.commands
    await batch.submit(items, reason.trim())
  }

  const retrying = Boolean(batch.submitted && batch.error)
  const busy = previewing || batch.busy

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
        <thead><tr><th className="pick-cell"><span className="sr-only">Select</span></th><th>Date</th><th>Description</th><th>Account</th><th className="amount">Native amount</th><th className="amount">Reported ({currency})</th></tr></thead>
        <tbody>
          {candidate.occurrences.map(o => {
            const reportedOccurrence = reportedByAnchor[o.anchor]
            return (
              <tr key={o.anchor}>
                <td className="pick-cell"><input type="checkbox" aria-label={`Include ${o.date}`} checked={selected.has(o.anchor)} onChange={() => toggle(o.anchor)} /></td>
                <td>{dateLabel(o.date)}</td>
                <td title={o.description}>{o.description || <span className="muted-note">No description</span>}</td>
                <td>{o.account}</td>
                <td className="amount">{money(o.amount, candidate.currency)}</td>
                <td className="amount">{reportedOccurrence?.reported ? <Money value={reportedOccurrence.reported} /> : <span className="muted-note">No evidence</span>}</td>
              </tr>
            )
          })}
        </tbody>
      </table>

      <form onSubmit={event => { event.preventDefault(); if (!preview) void reviewChanges(); else void saveChanges() }} onChange={invalidatePreview}>
        <div className="bulk-form">
          <div className="bulk-field">
            <CategoryPicker id={`income-category-${candidate.candidate}`} categories={categories} value={category}
              onChange={setCategory} disabled={disabled || busy} />
            <small className="field-context">Optional — leave blank to keep each receipt’s own category.</small>
          </div>
          <label>Classify as
            <select aria-label="Classification" value={classification} disabled={disabled || busy} onChange={e => setClassification(e.target.value)}>
              {CLASSIFICATIONS.map(c => <option key={c} value={c}>{INCOMING_LABELS[c] ?? c.replaceAll('_', ' ')}</option>)}
            </select>
          </label>
          <label>Reason<Input aria-label="Reason" required maxLength={2000} disabled={disabled || busy} value={reason} onChange={e => setReason(e.target.value)} placeholder="Why is this the right classification?" /></label>
        </div>
        <label className="inline-check"><input type="checkbox" checked={saveRule} disabled={disabled || busy} onChange={e => setSaveRule(e.target.checked)} /> Also save a reusable rule that <strong>suggests</strong> this classification for future credits whose description contains “{candidate.merchant_key}” — it never classifies them automatically</label>

        {previewError && <p className="form-error" role="alert">{previewError}</p>}
        {ruleError && <p className="form-error" role="alert">{ruleError}</p>}
        {batch.error && <p className="form-error" role="alert">{batch.error}</p>}
        {preview && <ClassificationPreviewSummary preview={preview} />}
        {ruleCompiled && <p className="muted-note" data-testid="rule-impact">
          Rule impact: suggests this classification for {ruleCompiled.impact.inbound_matches} matching credit{ruleCompiled.impact.inbound_matches === 1 ? '' : 's'} in
          total, {ruleCompiled.impact.still_unresolved} of which {ruleCompiled.impact.still_unresolved === 1 ? 'is' : 'are'} still unresolved today — it only
          suggests, so none of them are classified by saving this rule.
        </p>}
        {retrying && <p className="muted-note">The outcome may be unknown. Retry sends the same request safely; do not create a second decision.</p>}
        {disabled && <p className="form-error">Refresh the ledger before confirming income.</p>}
        <div className="dialog-actions">
          <Button type="submit" disabled={disabled || busy || !anchors.length || (!preview && !reason.trim()) || (Boolean(preview) && saveRule && !ruleCompiled)}>
            {previewing ? 'Checking…' : busy ? 'Working…' : preview ? (retrying ? 'Retry same save' : `Save ${preview.changed_transactions} receipt${preview.changed_transactions === 1 ? '' : 's'}${saveRule ? ' + rule' : ''}`) : 'Review changes'}
            {!previewing && !busy && <ArrowRight size={15} />}
          </Button>
          <span className="muted-note">{anchors.length ? `${candidate.currency} ${sumAmounts(chosen.map(o => o.amount))} (native)` : 'Select at least one receipt'}</span>
        </div>
      </form>
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
