import { useState } from 'react'
import { Check, Layers3, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { api, ApiError } from '@/lib/api'
import { useLedgerCommand } from '@/lib/command'
import { compileRule, type CompiledRule } from '@/features/rules/api'

const CHUNK = 200 // one atomic batch per <=200 items (Operations §1)

type Filters = {
  start: string; end: string; currency: string; search: string
  category: string; account: string; kind: string; include_excluded: boolean
}

/**
 * v0.5 slice 3 — bulk category assignment. Selection is either an explicit set
 * of page anchors or a frozen "all matching" materialization; once frozen, a
 * later filter change does not move what a commit touches. The commit is one
 * atomic DECISION_BATCH per 200-item chunk, each with its own request id, so a
 * large correction is several visible batches with no whole-job atomicity claim.
 */
export function BulkAssign({
  generation, freshness, categories, pageAnchors, picked, matchAll, total, filters,
  onSelectMatch, onClear, onSaved,
}: {
  generation: string
  freshness: string
  categories: string[]
  pageAnchors: string[]
  picked: Set<string>
  matchAll: { anchors: string[]; truncated: boolean } | null
  total: number
  filters: Filters
  onSelectMatch: (m: { anchors: string[]; truncated: boolean }) => void
  onClear: () => void
  onSaved: (status: string) => void | Promise<void>
}) {
  // A bulk override is durable; the picker must never choose a category on the
  // user's behalf merely because taxonomy data arrived first.
  const [category, setCategory] = useState('')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null)
  const [matching, setMatching] = useState(false)
  const [ruleOptions, setRuleOptions] = useState<Array<{ label: string; match: Record<string, unknown> }>>([])
  const [ruleOption, setRuleOption] = useState(0)
  const [ruleLoading, setRuleLoading] = useState(false)
  const [compiledRule, setCompiledRule] = useState<CompiledRule | null>(null)
  const ruleCommand = useLedgerCommand(generation, async status => { onClear(); await onSaved(status) })

  const anchors = matchAll ? matchAll.anchors : [...picked]
  const disabled = freshness === 'stale'
  const chunks = Math.ceil(anchors.length / CHUNK)

  async function selectAllMatching() {
    setMatching(true); setError('')
    try {
      const q = new URLSearchParams({ generation, currency: filters.currency, start: filters.start, end: filters.end })
      if (filters.search) q.set('search', filters.search)
      if (filters.category) q.set('category', filters.category)
      if (filters.account) q.set('account', filters.account)
      if (filters.kind) q.set('kind', filters.kind)
      if (filters.include_excluded) q.set('include_excluded', 'true')
      const result = await api<{ anchors: string[]; truncated: boolean }>('transactions/anchors?' + q)
      onSelectMatch(result)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setMatching(false)
    }
  }

  async function apply() {
    if (disabled || !anchors.length || busy) return
    setBusy(true); setError(''); setProgress({ done: 0, total: chunks })
    let pending = false
    let committed = 0
    try {
      for (let i = 0; i < anchors.length; i += CHUNK) {
        const slice = anchors.slice(i, i + CHUNK)
        const body = {
          request_id: crypto.randomUUID(),
          expected_generation: generation,
          reason: reason.trim(),
          items: slice.map(anchor => ({ kind: 'CATEGORY_OVERRIDE', anchors: { entry: anchor }, payload: { category, reason: reason.trim() } })),
        }
        const result = await api<{ status?: string }>('batch', body)
        if (result.status === 'recorded_refresh_pending') pending = true
        committed += 1
        setProgress({ done: committed, total: chunks })
      }
      onClear()
      await onSaved(pending ? 'recorded_refresh_pending' : 'applied')
    } catch (e) {
      // The draft (category + reason) is kept; already-committed chunks stand.
      const message = e instanceof ApiError ? e.message : (e as Error).message
      setError(committed > 0
        ? `${message} — ${committed} of ${chunks} batch${chunks === 1 ? '' : 'es'} were already applied; narrow the selection before retrying.`
        : message)
    } finally {
      setBusy(false)
    }
  }

  async function suggestRule() {
    if (!anchors.length || ruleLoading) return
    setRuleLoading(true); setError(''); setRuleOptions([]); setCompiledRule(null); ruleCommand.reset()
    try {
      const result = await api<{ options: Array<{ label: string; match: Record<string, unknown> }>; unavailable_reason?: string | null }>('categorization/derive-rule', { generation, anchors })
      setRuleOptions(result.options)
      if (!result.options.length) setError(result.unavailable_reason || 'This selection does not share a safe reusable predicate.')
    } catch (e) { setError((e as Error).message) } finally { setRuleLoading(false) }
  }

  async function checkRuleImpact() {
    if (!ruleOptions[ruleOption] || !category || !reason.trim()) return
    setRuleLoading(true); setError(''); setCompiledRule(null); ruleCommand.reset()
    try { setCompiledRule(await compileRule(ruleOptions[ruleOption].match, category, reason.trim())) }
    catch (e) { setError((e as Error).message) } finally { setRuleLoading(false) }
  }

  return (
    <div className="bulk-assign" role="region" aria-label="Bulk category assignment">
      <div className="bulk-head">
        <span><Layers3 size={15} /> <strong>{anchors.length}</strong> selected{matchAll ? ' (all matching)' : ''}{matchAll?.truncated ? ` · capped, narrow the filter` : ''}</span>
        {!matchAll && picked.size > 0 && picked.size < total && (
          <Button variant="ghost" size="sm" disabled={matching} onClick={selectAllMatching}>{matching ? 'Selecting…' : `Select all ${total} matching`}</Button>
        )}
        <Button variant="ghost" size="sm" onClick={onClear}><X size={13} /> Clear</Button>
      </div>
      <div className="bulk-form">
        <label>Set category
          <select value={category} disabled={busy} onChange={e => { setCategory(e.target.value); setCompiledRule(null); ruleCommand.reset() }}>
            <option value="" disabled>Choose a category</option>
            {categories.filter(c => !c.startsWith('Internal')).map(c => <option key={c}>{c}</option>)}
          </select>
        </label>
        <label>Reason <small>(optional for one-off overrides; required for a reusable rule)</small><Input aria-label="Reason" maxLength={2000} value={reason} disabled={busy} onChange={e => { setReason(e.target.value); setCompiledRule(null); ruleCommand.reset() }} placeholder="Optional note for your history" /></label>
        <Button type="button" disabled={disabled || busy || !anchors.length || !category} onClick={apply}>
          {busy ? `Applying ${progress?.done ?? 0}/${progress?.total ?? chunks}…` : `Apply to ${anchors.length}${chunks > 1 ? ` in ${chunks} batches` : ''}`}
          {!busy && <Check size={15} />}
        </Button>
      </div>
      <div className="bulk-rule-suggestion">
        {!ruleOptions.length && <Button variant="outline" type="button" disabled={disabled || ruleLoading || !anchors.length} onClick={() => void suggestRule()}>{ruleLoading ? 'Checking selection…' : 'Suggest reusable rule'}</Button>}
        {ruleOptions.length > 0 && <>
          <p className="muted-note">A reusable rule affects matching transactions now and future imports. It does not create one-off overrides for this selection.</p>
          {ruleOptions.length > 1 && <label>Reusable match<select value={ruleOption} onChange={e => { setRuleOption(Number(e.target.value)); setCompiledRule(null); ruleCommand.reset() }}>{ruleOptions.map((option, index) => <option key={option.label} value={index}>{option.label}</option>)}</select></label>}
          {compiledRule && !ruleCommand.previewed && <p className="muted-note">{compiledRule.impact.newly_categorized + compiledRule.impact.recategorized} transactions will change; {compiledRule.impact.protected_by_override} manual overrides stay protected.</p>}
          {ruleCommand.error && <p className="form-error" role="alert">{ruleCommand.error}</p>}
          <Button variant="outline" type="button" disabled={disabled || ruleLoading || ruleCommand.busy || !category || !reason.trim()} onClick={() => { if (compiledRule) void ruleCommand.act(compiledRule.command); else void checkRuleImpact() }}>
            {ruleLoading ? 'Checking impact…' : !compiledRule ? 'Check reusable-rule impact' : ruleCommand.previewed ? (ruleCommand.attempted ? 'Retry same save' : 'Save reusable rule') : 'Preview reusable rule'}
          </Button>
        </>}
      </div>
      {chunks > 1 && !busy && <p className="muted-note">This runs {chunks} atomic batches of up to {CHUNK}. Each batch is all-or-nothing; the set of batches is not.</p>}
      {disabled && <p className="form-error">Refresh the ledger before a bulk correction.</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
    </div>
  )
}
