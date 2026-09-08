import { useCallback, useEffect, useState } from 'react'
import { ChevronRight, RefreshCw, Tag } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { api } from '@/lib/api'
import { useLedgerCommand } from '@/lib/command'
import { compileRule, type CompiledRule } from '@/features/rules/api'
import { ImpactDetails } from '@/features/rules'

type Summary = { counts: Record<string, number>; active_categorization_denominator: number; progress_numerator: number; needs_attention: number }
type Candidate = { candidate_id: string; kind: string; label: string; transaction_count: number; active_count: number; date_range: { first: string; last: string }; accounts: string[]; directions: Record<string, number>; examples: string[] }
type CandidatesPage = { candidates: Candidate[]; next_cursor: string | null; total: number }
type CandidateRecord = { anchor: string; description: string | null; account: string; currency: string; amount: string; date: string; direction: string; active: boolean; merchant_raw?: string | null; merchant_normalized?: string | null }
type Detail = { candidate: Candidate; records: CandidateRecord[]; next_cursor?: string | null }
type RuleOption = { label: string; match: Record<string, unknown> }

export function CategorizationView({ generation, freshness, categories, onSaved }: {
  generation: string; freshness: string; categories: string[]; onSaved: (status: string) => void | Promise<void>
}) {
  const [summary, setSummary] = useState<Summary | null>(null)
  const [page, setPage] = useState<CandidatesPage | null>(null)
  const [selected, setSelected] = useState<Detail | null>(null)
  const [error, setError] = useState('')
  const [direction, setDirection] = useState('')
  const [lifecycle, setLifecycle] = useState('')
  const [kind, setKind] = useState('')
  const [account, setAccount] = useState('')
  const [currency, setCurrency] = useState('')
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const filters = new URLSearchParams({ direction, lifecycle, kind, account, currency, start, end })
  function candidateQuery(cursor = '') {
    const query = new URLSearchParams({ generation, ...Object.fromEntries(filters) })
    if (cursor) query.set('cursor', cursor)
    return query.toString()
  }
  const load = useCallback(async () => {
    setError('')
    try {
      const [s, p] = await Promise.all([
        api<Summary>(`categorization/summary?generation=${encodeURIComponent(generation)}`),
        api<CandidatesPage>(`categorization/candidates?${candidateQuery()}`),
      ])
      setSummary(s); setPage(p)
    } catch (e) { setError((e as Error).message) }
  }, [generation, direction, lifecycle, kind, account, currency, start, end])
  useEffect(() => { void load() }, [load])
  async function open(candidate: Candidate) {
    setError('')
    try { setSelected(await api<Detail>(`categorization/candidates/${candidate.candidate_id}?${candidateQuery()}`)) }
    catch (e) { setError((e as Error).message) }
  }
  async function loadMoreCandidates() {
    if (!page?.next_cursor) return
    try {
      const more = await api<CandidatesPage>(`categorization/candidates?${candidateQuery(page.next_cursor)}`)
      setPage(current => current ? { ...more, candidates: [...current.candidates, ...more.candidates], total: current.total } : more)
    } catch (e) { setError((e as Error).message) }
  }
  const disabled = freshness === 'stale'
  return <div className="categorization-view">
    {error && <div className="alert error" role="alert">{error}</div>}
    <div className="scope-caption"><span>Group similar unclassified transactions, then make a deliberate one-off or reusable decision.</span><span className="categorization-filters"><select aria-label="Filter categorization candidate kind" value={kind} onChange={e => setKind(e.target.value)}><option value="">All group types</option><option value="merchant">Merchant</option><option value="paynow_recipient">PayNow recipient</option><option value="description">Description</option><option value="missing_merchant">Missing merchant</option></select><select aria-label="Filter categorization direction" value={direction} onChange={e => setDirection(e.target.value)}><option value="">All directions</option><option value="OUT">Outgoing</option><option value="IN">Incoming</option></select><select aria-label="Filter categorization lifecycle" value={lifecycle} onChange={e => setLifecycle(e.target.value)}><option value="">All lifecycle states</option><option value="COMPLETED">Active only</option><option value="PENDING">Pending</option><option value="REVERTED">Reverted</option></select><input aria-label="Filter categorization account" value={account} onChange={e => setAccount(e.target.value)} placeholder="Account ID"/><input aria-label="Filter categorization currency" value={currency} onChange={e => setCurrency(e.target.value.toUpperCase())} maxLength={3} placeholder="Currency"/><input aria-label="Filter categorization start date" type="date" value={start} onChange={e => setStart(e.target.value)}/><input aria-label="Filter categorization end date" type="date" value={end} onChange={e => setEnd(e.target.value)}/><Button variant="ghost" size="sm" onClick={() => void load()}><RefreshCw size={14}/>Refresh</Button></span></div>
    {summary && <CoverageSummary summary={summary}/>} 
    <Card className="candidate-list">
      <div className="panel-title"><div><p className="eyebrow">CATEGORIZATION INBOX</p><h2>{page ? `${page.total} groups need review` : 'Loading groups…'}</h2></div></div>
      {!page?.candidates.length && <p className="empty-line">Everything active is categorized, or no groups match the current ledger generation.</p>}
      {page?.candidates.map(candidate => <button className="candidate-card" key={candidate.candidate_id} onClick={() => void open(candidate)}>
        <span className="merchant-icon"><Tag size={16}/></span><span className="candidate-main"><strong>{candidate.label}</strong><small>{candidate.transaction_count} transactions · {candidate.active_count} active · {candidate.date_range.first} to {candidate.date_range.last}</small><small>{candidate.examples.slice(0, 2).join(' · ') || candidate.kind.replace('_', ' ')}</small></span><ChevronRight size={16}/>
      </button>)}
      {page?.next_cursor && <div className="dialog-actions"><Button variant="outline" onClick={() => void loadMoreCandidates()}>Load more groups</Button></div>}
    </Card>
    {selected && <CandidateReview detail={selected} generation={generation} categories={categories} disabled={disabled} onClose={() => setSelected(null)} onSaved={async status => { await onSaved(status); setSelected(null); await load() }}/>} 
  </div>
}

function CoverageSummary({ summary }: { summary: Summary }) {
  const percent = summary.active_categorization_denominator ? Math.round(summary.progress_numerator / summary.active_categorization_denominator * 100) : 100
  return <div className="coverage-summary"><Card><small>Rule-assigned</small><strong>{summary.counts.rule_assigned || 0}</strong></Card><Card><small>Manually assigned</small><strong>{summary.counts.manual_override || 0}</strong></Card><Card><small>Needs a category</small><strong>{summary.counts.unclassified_active || 0}</strong></Card><Card><small>Progress</small><strong>{percent}%</strong><small>{summary.progress_numerator} / {summary.active_categorization_denominator}</small></Card>{summary.needs_attention > 0 && <Card><small>Needs attention</small><strong>{summary.needs_attention}</strong></Card>}</div>
}

function CandidateReview({ detail, generation, categories, disabled, onClose, onSaved }: { detail: Detail; generation: string; categories: string[]; disabled: boolean; onClose: () => void; onSaved: (status: string) => void | Promise<void> }) {
  const [scope, setScope] = useState<'one'|'selected'|'rule'>('rule')
  const [anchors, setAnchors] = useState(() => new Set(detail.records.map(row => row.anchor)))
  const [records, setRecords] = useState(detail.records)
  const [nextCursor, setNextCursor] = useState(detail.next_cursor ?? null)
  const [options, setOptions] = useState<RuleOption[]>([])
  const [option, setOption] = useState(0)
  const [category, setCategory] = useState('')
  const [reason, setReason] = useState('')
  const [compiled, setCompiled] = useState<CompiledRule | null>(null)
  const [combinedImpact, setCombinedImpact] = useState<CompiledRule['impact'] | null>(null)
  const [error, setError] = useState('')
  const [batching, setBatching] = useState(false)
  const [detailLoading, setDetailLoading] = useState(false)
  const variants = [...new Set(detail.records.map(row => row.merchant_normalized).filter((value): value is string => Boolean(value)))]
  const [aliasVariants, setAliasVariants] = useState<Set<string>>(() => new Set())
  const [aliasCanonical, setAliasCanonical] = useState(detail.candidate.label)
  const command = useLedgerCommand(generation, onSaved)
  useEffect(() => {
    api<{ options: RuleOption[] }>('categorization/derive-rule', { generation, anchors: detail.records.map(r => r.anchor) })
      .then(result => { setOptions(result.options); if (!result.options.length) setScope('one') })
      .catch(e => setError((e as Error).message))
  }, [detail, generation])
  async function previewRule() {
    if (!options[option] || !category || !reason.trim()) return
    setError(''); setCompiled(null); setCombinedImpact(null); command.reset()
    try {
      if (aliasVariants.size) {
        const preview = await api<{ impact: CompiledRule['impact'] }>('categorization/impact', {
          generation, match: options[option].match, category, reason: reason.trim(),
          alias_canonical: aliasCanonical.trim(), alias_patterns: [...aliasVariants].map(exact => ({ exact })),
        })
        setCombinedImpact(preview.impact)
      }
      setCompiled(await compileRule(options[option].match, category, reason.trim()))
    } catch (e) { setError((e as Error).message) }
  }
  async function applySelected() {
    if (!category || !anchors.size || batching) return
    setBatching(true); setError('')
    try {
      const result = await api<{ status: string }>('batch', {
        request_id: crypto.randomUUID(), expected_generation: generation, reason: reason.trim(),
        items: [...anchors].map(entry => ({ kind: 'CATEGORY_OVERRIDE', anchors: { entry }, payload: { category, reason: reason.trim() } })),
      })
      await onSaved(result.status)
    } catch (e) { setError((e as Error).message) } finally { setBatching(false) }
  }
  async function loadMoreRecords() {
    if (!nextCursor || detailLoading) return
    setDetailLoading(true)
    try {
      const more = await api<Detail>(`categorization/candidates/${encodeURIComponent(detail.candidate.candidate_id)}?generation=${encodeURIComponent(generation)}&cursor=${encodeURIComponent(nextCursor)}`)
      // The draft's initially frozen selection is intentionally not expanded;
      // additional records are inspectable, then explicitly selectable.
      setRecords(current => [...current, ...more.records])
      setNextCursor(more.next_cursor ?? null)
    } catch (e) { setError((e as Error).message) } finally { setDetailLoading(false) }
  }
  async function saveRuleWithAlias() {
    if (!compiled || !aliasVariants.size || batching) return
    setBatching(true); setError('')
    try {
      const reasonText = reason.trim()
      const result = await api<{ status: string }>('batch', {
        request_id: crypto.randomUUID(), expected_generation: generation, reason: reasonText,
        items: [
          { kind: 'MERCHANT_ALIAS_SET', anchors: { canonical: aliasCanonical.trim() }, payload: { patterns: [...aliasVariants].map(exact => ({ exact })), reason: reasonText } },
          compiled.command,
        ],
      })
      await onSaved(result.status)
    } catch (e) { setError((e as Error).message) } finally { setBatching(false) }
  }
  const record = detail.records[0]
  return <Card className="candidate-review" aria-label={`Review ${detail.candidate.label}`}>
    <div className="panel-title"><div><p className="eyebrow">REVIEW GROUP</p><h2>{detail.candidate.label}</h2></div><Button variant="ghost" size="sm" onClick={onClose}>Close</Button></div>
    <div className="candidate-records">{records.map(row => <label key={row.anchor}><input type="checkbox" checked={anchors.has(row.anchor)} onChange={() => setAnchors(current => { const next = new Set(current); next.has(row.anchor) ? next.delete(row.anchor) : next.add(row.anchor); return next })}/><span>{row.date} · {row.account}</span><strong>{row.currency} {row.amount}</strong><small>{row.description || 'No description'}</small></label>)}</div>
    {nextCursor && <Button variant="outline" size="sm" disabled={detailLoading} onClick={() => void loadMoreRecords()}>{detailLoading ? 'Loading records…' : 'Load more records'}</Button>}
    <fieldset disabled={disabled || command.attempted || batching}><legend>Choose scope</legend><label><input type="radio" checked={scope === 'one'} onChange={() => { setScope('one'); setCompiled(null); command.reset() }}/> Only this transaction</label><label><input type="radio" checked={scope === 'selected'} onChange={() => { setScope('selected'); setCompiled(null); command.reset() }}/> Selected transactions ({anchors.size})</label><label><input type="radio" checked={scope === 'rule'} disabled={!options.length} onChange={() => { setScope('rule'); setCompiled(null); command.reset() }}/> All matching transactions, now and later</label></fieldset>
    {scope === 'rule' && options.length > 1 && <label>Match scope<select value={option} onChange={e => { setOption(Number(e.target.value)); setCompiled(null); command.reset() }}>{options.map((item, index) => <option key={item.label} value={index}>{item.label}</option>)}</select></label>}
    {scope === 'rule' && variants.length > 0 && <fieldset className="alias-step"><legend>Normalize statement-name variants <small>(optional)</small></legend><label>Canonical merchant name<input value={aliasCanonical} onChange={e => setAliasCanonical(e.target.value)} /></label>{variants.map(variant => <label key={variant}><input type="checkbox" checked={aliasVariants.has(variant)} onChange={() => setAliasVariants(current => { const next = new Set(current); next.has(variant) ? next.delete(variant) : next.add(variant); return next })}/>{variant}</label>)}</fieldset>}
    <label>Category<select value={category} onChange={e => { setCategory(e.target.value); setCompiled(null); command.reset() }}><option value="" disabled>Choose a category</option>{categories.filter(c => !c.startsWith('Internal /')).map(c => <option key={c}>{c}</option>)}</select></label>
    <label>Reason {(scope === 'one' || scope === 'selected') && <small>(optional)</small>}<input value={reason} onChange={e => { setReason(e.target.value); setCompiled(null); command.reset() }} /></label>
    {error && <p className="form-error">{error}</p>}{command.error && <p className="form-error">{command.error}</p>}
    {compiled && !command.previewed && <ImpactDetails impact={combinedImpact ?? compiled.impact}/>} 
    <div className="dialog-actions">{scope === 'one' ? <Button disabled={disabled || !category || command.busy} onClick={() => void command.act({ kind: 'CATEGORY_OVERRIDE', anchors: { entry: record.anchor }, payload: { category, reason } })}>{command.previewed ? 'Save one transaction' : 'Preview one transaction'}</Button> : scope === 'selected' ? <Button disabled={disabled || !category || !anchors.size || batching} onClick={() => void applySelected()}>{batching ? 'Saving selected transactions…' : `Save ${anchors.size} selected transactions`}</Button> : aliasVariants.size ? <Button disabled={disabled || !compiled || !aliasCanonical.trim() || batching} onClick={() => void saveRuleWithAlias()}>{batching ? 'Saving alias and rule…' : 'Save alias and rule together'}</Button> : <Button disabled={disabled || !category || !reason.trim() || command.busy} onClick={() => { if (compiled) void command.act(compiled.command); else void previewRule() }}>{!compiled ? 'Check exact impact' : command.previewed ? 'Save rule' : 'Preview rule'}</Button>}</div>
  </Card>
}
