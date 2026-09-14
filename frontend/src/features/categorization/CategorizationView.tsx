import { useCallback, useEffect, useRef, useState } from 'react'
import { ChevronRight, Filter, HelpCircle, RefreshCw, Tag, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { api, money } from '@/lib/api'
import { useBatchCommand } from '@/lib/command'
import { compileCombinedRule, type CompiledCombinedRule } from '@/features/rules/api'
import { CombinedImpactSummary, MergeMerchantsDialog, MerchantRenameControl } from '@/features/rules'
import { loadRecentCategories, rememberCategory } from '@/features/classification/CategoryPicker'
import { ClassificationFieldsFieldset } from '@/features/classification/FieldsFieldset'
import { ClassificationPreviewSummary } from '@/features/classification/PreviewSummary'
import { PresetPicker } from '@/features/classification/PresetPicker'
import { RuleSection } from '@/features/classification/RuleSection'
import type { PresetFields } from '@/features/classification/presets'
import { previewClassification, COST_BEHAVIORS, NEEDS, INCOMING_TYPES, INCOMING_LABELS, label, type Applicability, type ClassificationCommand, type ClassificationDraftBody, type ClassificationPreview, type FieldName } from '@/features/classification/api'

type Summary = {
  counts: Record<string, number>; active_categorization_denominator: number; progress_numerator: number
  needs_attention: number; combined_completeness?: { numerator: number; denominator: number }
}
type Candidate = { candidate_id: string; kind: string; label: string; merchant_key?: string; transaction_count: number; active_count: number; date_range: { first: string; last: string }; accounts: string[]; directions: Record<string, number>; currencies?: Record<string, number>; examples: string[] }
type CandidatesPage = { candidates: Candidate[]; next_cursor: string | null; total: number }
type CandidateRecord = { anchor: string; description: string | null; account: string; currency: string; amount: string; date: string; direction: string; active: boolean; merchant_raw?: string | null; merchant_normalized?: string | null; detail_bucket?: string; applicable_fields?: Applicability; effective?: Record<string, string | null>; conflicts?: { ambiguous: boolean; direction: boolean } }
type Detail = { candidate: Candidate; records: CandidateRecord[]; next_cursor?: string | null }
type RuleOption = { label: string; match: Record<string, unknown> }
type UndoEntry = { id: string; message: string; undo: () => Promise<void> }

// Quick-discovery focuses (PLAN-unified-transaction-classification.md Step 5).
// "category" is the original inbox; the others group by an authoritative
// *effective* dimension computed server-side across the full result set, not
// just the currently loaded page (`fincl.categorization_queries._focus_groups`).
const FOCUSES: { value: string; label: string }[] = [
  { value: 'category', label: 'Missing category' },
  { value: 'spending_details', label: 'Missing spending details' },
  { value: 'incoming', label: 'Incoming money to classify' },
  { value: 'conflicts', label: 'Conflicts' },
]

function cycle(options: readonly string[], current: string): string {
  const values = ['', ...options]
  const index = values.indexOf(current)
  return values[(index + 1) % values.length]
}

export function CategorizationView({ generation, freshness, categories, accounts = [], currencies = [], onSaved }: {
  generation: string; freshness: string; categories: string[]; accounts?: string[]; currencies?: string[]; onSaved: (status: string) => void | Promise<void>
}) {
  const [summary, setSummary] = useState<Summary | null>(null)
  const [page, setPage] = useState<CandidatesPage | null>(null)
  const [focusCounts, setFocusCounts] = useState<Record<string, number>>({})
  const [selected, setSelected] = useState<Detail | null>(null)
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null)
  const [activeIndex, setActiveIndex] = useState(0)
  const [error, setError] = useState('')
  const [direction, setDirection] = useState('')
  const [lifecycle, setLifecycle] = useState('')
  const [kind, setKind] = useState('')
  const [account, setAccount] = useState('')
  const [currency, setCurrency] = useState('')
  const [focus, setFocus] = useState('category')
  const [undoEntry, setUndoEntry] = useState<UndoEntry | null>(null)
  function candidateQuery(cursor = '', focusOverride?: string) {
    const query = new URLSearchParams({ generation, direction, lifecycle, kind, account, currency, focus: focusOverride ?? focus })
    if (cursor) query.set('cursor', cursor)
    return query.toString()
  }
  const load = useCallback(async () => {
    setError('')
    try {
      const otherFocuses = FOCUSES.filter(f => f.value !== focus)
      const [s, p, ...others] = await Promise.all([
        api<Summary>(`categorization/summary?generation=${encodeURIComponent(generation)}`),
        api<CandidatesPage>(`categorization/candidates?${candidateQuery()}`),
        ...otherFocuses.map(f => api<CandidatesPage>(`categorization/candidates?${candidateQuery('', f.value)}`)),
      ])
      setSummary(s); setPage(p)
      setFocusCounts({ [focus]: p.total, ...Object.fromEntries(otherFocuses.map((f, i) => [f.value, others[i].total])) })
      return p
    } catch (e) { setError((e as Error).message); return null }
  }, [generation, direction, lifecycle, kind, account, currency, focus])
  useEffect(() => { void load() }, [load])
  const queue = page?.candidates ?? []
  async function open(candidate: Candidate, index: number) {
    setError('')
    try { setSelected(await api<Detail>(`categorization/candidates/${candidate.candidate_id}?${candidateQuery()}`)); setSelectedIndex(index); setActiveIndex(index) }
    catch (e) { setError((e as Error).message) }
  }
  async function loadMoreCandidates() {
    if (!page?.next_cursor) return
    try {
      const more = await api<CandidatesPage>(`categorization/candidates?${candidateQuery(page.next_cursor)}`)
      setPage(current => current ? { ...more, candidates: [...current.candidates, ...more.candidates], total: current.total } : more)
    } catch (e) { setError((e as Error).message) }
  }
  function onListKeyDown(e: React.KeyboardEvent) {
    if (!queue.length) return
    if (e.key === 'ArrowDown' || e.key === 'j') { e.preventDefault(); setActiveIndex(i => Math.min(i + 1, queue.length - 1)) }
    else if (e.key === 'ArrowUp' || e.key === 'k') { e.preventDefault(); setActiveIndex(i => Math.max(i - 1, 0)) }
    else if (e.key === 'Enter') { e.preventDefault(); const candidate = queue[activeIndex]; if (candidate) void open(candidate, activeIndex) }
  }
  /** Save-and-next recomputes its target against the *freshly refreshed*
   * queue, never the pre-save `queue` closure: a stale-indexing bug would
   * otherwise silently skip or reopen the wrong group whenever a save
   * removes the current candidate (the common case -- it is now resolved)
   * and shifts every later one up by one position. Prefers reopening the
   * candidate that was next before the save, found by id so a resort does
   * not defeat it; falls back to whatever now sits at the old index (the
   * usual case once the current group disappears); shows the existing empty
   * state once the queue is exhausted. */
  async function afterSave(status: string) {
    await onSaved(status)
    const wasNextId = selectedIndex !== null ? (queue[selectedIndex + 1]?.candidate_id ?? null) : null
    const oldIndex = selectedIndex
    const freshPage = await load()
    const freshQueue = freshPage?.candidates ?? []
    let nextIndex = wasNextId ? freshQueue.findIndex(c => c.candidate_id === wasNextId) : -1
    if (nextIndex === -1 && oldIndex !== null) nextIndex = oldIndex
    if (nextIndex >= 0 && freshQueue[nextIndex]) await open(freshQueue[nextIndex], nextIndex)
    else { setSelected(null); setSelectedIndex(null) }
  }
  const disabled = freshness === 'stale'
  return <div className="categorization-view">
    {error && <div className="alert error" role="alert">{error}</div>}
    <div className="scope-caption">
      <span>Group similar unclassified transactions, then make a deliberate one-off or reusable decision. <kbd>j</kbd>/<kbd>k</kbd> to move, <kbd>Enter</kbd> to open.</span>
      <span className="categorization-filters">
        <FiltersPopover
          kind={kind} setKind={setKind} direction={direction} setDirection={setDirection}
          lifecycle={lifecycle} setLifecycle={setLifecycle} account={account} setAccount={setAccount} accounts={accounts}
          currency={currency} setCurrency={setCurrency} currencies={currencies}
        />
        <Button variant="ghost" size="sm" onClick={() => void load()}><RefreshCw size={14}/>Refresh</Button>
      </span>
    </div>
    <FocusTabs focus={focus} counts={focusCounts} onChange={setFocus}/>
    {summary && <CoverageSummary summary={summary}/>}
    <div className="categorization-split">
      <Card className="candidate-list" onKeyDown={onListKeyDown}>
        <div className="panel-title"><div><p className="eyebrow"><span>CATEGORIZATION INBOX</span> · ALL SELECTED HISTORY</p><h2>{page ? (focus === 'category' ? `${page.total} groups need review` : `${page.total} groups match ${FOCUSES.find(f => f.value === focus)?.label.toLowerCase()}`) : 'Loading groups…'}</h2><p className="muted-note">Covers every imported statement, all dates and currencies. Rules also apply to future imports. Pending, reverted and superseded rows are shown but not counted as active.</p></div></div>
        {!page?.candidates.length && <p className="empty-line">{focus === 'category' ? 'Everything active is categorized, or no groups match the current ledger generation.' : 'Nothing active matches this focus, or no groups match the current ledger generation.'}</p>}
        {page?.candidates.map((candidate, index) => <button className={`candidate-card${index === activeIndex ? ' is-active' : ''}`} key={candidate.candidate_id} tabIndex={index === activeIndex ? 0 : -1} onFocus={() => setActiveIndex(index)} onClick={() => void open(candidate, index)}>
          <span className="merchant-icon"><Tag size={16}/></span><span className="candidate-main"><strong>{candidate.label}</strong><small>{candidate.transaction_count} transactions · {candidate.active_count} active · {candidate.date_range.first} to {candidate.date_range.last}</small><small>{candidate.examples.slice(0, 2).join(' · ') || candidate.kind.replace('_', ' ')}</small>{candidate.directions.IN > 0 && candidate.directions.OUT > 0 && <small className="tag">includes {candidate.directions.IN} incoming</small>}{candidate.currencies && Object.keys(candidate.currencies).length > 1 && <small className="tag">{Object.keys(candidate.currencies).length} currencies</small>}</span><ChevronRight size={16}/>
        </button>)}
        {page?.next_cursor && <div className="dialog-actions"><Button variant="outline" onClick={() => void loadMoreCandidates()}>Load more groups</Button></div>}
      </Card>
      {selected
        ? <CandidateReview
            key={selected.candidate.candidate_id}
            detail={selected} generation={generation} categories={categories} disabled={disabled}
            hasNext={selectedIndex !== null && selectedIndex + 1 < queue.length}
            onClose={() => { setSelected(null); setSelectedIndex(null) }}
            onNavigate={async delta => { if (selectedIndex !== null && queue[selectedIndex + delta]) await open(queue[selectedIndex + delta], selectedIndex + delta) }}
            onSaved={afterSave}
            onUndoable={setUndoEntry}
          />
        : <Card className="categorization-panel categorization-panel-empty"><p className="empty-line">Select a group from the list to review it here.</p></Card>}
    </div>
    {undoEntry && <UndoToast entry={undoEntry} onDone={() => setUndoEntry(null)}/>}
  </div>
}

function UndoToast({ entry, onDone }: { entry: UndoEntry; onDone: () => void }) {
  const [working, setWorking] = useState(false)
  useEffect(() => { const timer = setTimeout(onDone, 10_000); return () => clearTimeout(timer) }, [entry.id, onDone])
  return <div className="undo-toast" role="status">
    <span>{entry.message}</span>
    <Button type="button" variant="ghost" size="sm" disabled={working} onClick={async () => { setWorking(true); try { await entry.undo() } finally { setWorking(false); onDone() } }}>
      {working ? 'Undoing…' : 'Undo'}
    </Button>
  </div>
}

function FocusTabs({ focus, counts, onChange }: { focus: string; counts: Record<string, number>; onChange: (value: string) => void }) {
  return <div className="focus-tabs" role="tablist" aria-label="Categorization focus">
    {FOCUSES.map(f => <button key={f.value} type="button" role="tab" aria-selected={focus === f.value}
      className={focus === f.value ? 'is-active' : ''} onClick={() => onChange(f.value)}>
      {f.label}{counts[f.value] !== undefined && <span className="tag">{counts[f.value]}</span>}
    </button>)}
  </div>
}

function FiltersPopover({ kind, setKind, direction, setDirection, lifecycle, setLifecycle, account, setAccount, accounts, currency, setCurrency, currencies }: {
  kind: string; setKind: (v: string) => void; direction: string; setDirection: (v: string) => void
  lifecycle: string; setLifecycle: (v: string) => void; account: string; setAccount: (v: string) => void; accounts: string[]
  currency: string; setCurrency: (v: string) => void; currencies: string[]
}) {
  const [open, setOpen] = useState(false)
  const chips: { key: string; text: string; clear: () => void }[] = []
  if (kind) chips.push({ key: 'kind', text: kind.replace('_', ' '), clear: () => setKind('') })
  if (direction) chips.push({ key: 'direction', text: direction === 'OUT' ? 'Outgoing' : 'Incoming', clear: () => setDirection('') })
  if (lifecycle) chips.push({ key: 'lifecycle', text: lifecycle, clear: () => setLifecycle('') })
  if (account) chips.push({ key: 'account', text: account, clear: () => setAccount('') })
  if (currency) chips.push({ key: 'currency', text: currency, clear: () => setCurrency('') })
  return <span className="filters-popover">
    <Button type="button" variant="outline" size="sm" onClick={() => setOpen(o => !o)}><Filter size={13}/>Filters{chips.length > 0 ? ` (${chips.length})` : ''}</Button>
    {open && <div className="filters-popover-panel">
      <select aria-label="Filter categorization candidate kind" value={kind} onChange={e => setKind(e.target.value)}><option value="">All group types</option><option value="merchant">Merchant</option><option value="paynow_recipient">PayNow recipient</option><option value="description">Description</option><option value="missing_merchant">Missing merchant</option></select>
      <select aria-label="Filter categorization direction" value={direction} onChange={e => setDirection(e.target.value)}><option value="">All directions</option><option value="OUT">Outgoing</option><option value="IN">Incoming</option></select>
      <select aria-label="Filter categorization lifecycle" value={lifecycle} onChange={e => setLifecycle(e.target.value)}><option value="">All lifecycle states</option><option value="COMPLETED">Active only</option><option value="PENDING">Pending</option><option value="REVERTED">Reverted</option></select>
      <select aria-label="Filter categorization account" value={account} onChange={e => setAccount(e.target.value)}><option value="">All accounts</option>{accounts.map(a => <option key={a}>{a}</option>)}</select>
      <select aria-label="Filter categorization currency" value={currency} onChange={e => setCurrency(e.target.value)}><option value="">All currencies</option>{currencies.map(c => <option key={c}>{c}</option>)}</select>
    </div>}
    {chips.length > 0 && <span className="filter-chips">{chips.map(chip => <button type="button" key={chip.key} className="filter-chip" onClick={chip.clear}>{chip.text}<X size={11}/></button>)}</span>}
  </span>
}

function CoverageSummary({ summary }: { summary: Summary }) {
  const percent = summary.active_categorization_denominator ? Math.round(summary.progress_numerator / summary.active_categorization_denominator * 100) : 100
  const combined = summary.combined_completeness
  const combinedPercent = combined?.denominator ? Math.round(combined.numerator / combined.denominator * 100) : 100
  return <Card className="progress-summary">
    <div className="progress-summary-head"><strong>{summary.progress_numerator} of {summary.active_categorization_denominator} active transactions fully classified</strong><span>{percent}%</span></div>
    <div className="progress-track"><span style={{ width: `${percent}%` }}/></div>
    <details className="progress-details">
      <summary>Details</summary>
      <div className="coverage-summary">
        <Card><small>Rule-assigned</small><strong>{summary.counts.rule_assigned || 0}</strong></Card>
        <Card><small>Manually assigned</small><strong>{summary.counts.manual_override || 0}</strong></Card>
        <Card><small>Needs a category</small><strong>{summary.counts.unclassified_active || 0}</strong></Card>
        {combined && <Card><small>Combined completeness</small><strong>{combinedPercent}%</strong><small>{combined.numerator} / {combined.denominator} — category, spending pattern/need, and incoming type where each applies</small></Card>}
        {summary.needs_attention > 0 && <Card><small>Needs attention</small><strong>{summary.needs_attention}</strong></Card>}
      </div>
    </details>
  </Card>
}

function CategoryCombobox({ id, categories, value, onChange, onCreate, disabled }: {
  id: string; categories: string[]; value: string; onChange: (category: string) => void; onCreate: (name: string) => Promise<boolean>; disabled?: boolean
}) {
  const options = categories.filter(c => !c.startsWith('Internal /'))
  const recent = loadRecentCategories().filter(c => options.includes(c))
  const ordered = [...new Set([...recent, ...options])]
  const [query, setQuery] = useState(value)
  const [open, setOpen] = useState(false)
  const [highlight, setHighlight] = useState(0)
  const [creating, setCreating] = useState(false)
  useEffect(() => { setQuery(value) }, [value])
  const filtered = query.trim() ? ordered.filter(c => c.toLowerCase().includes(query.trim().toLowerCase())) : ordered
  function choose(category: string) { onChange(category); setQuery(category); setOpen(false) }
  async function createFromQuery() {
    if (creating) return
    setCreating(true)
    try { if (await onCreate(query.trim())) choose(query.trim()) }
    finally { setCreating(false) }
  }
  return <div className="category-combobox">
    <label htmlFor={id}>Category</label>
    <input id={id} role="combobox" aria-expanded={open} aria-autocomplete="list" aria-controls={`${id}-list`} autoComplete="off"
      disabled={disabled} value={query} placeholder="Search or create a category…"
      onChange={e => { setQuery(e.target.value); setOpen(true); setHighlight(0) }}
      onFocus={() => setOpen(true)}
      onBlur={() => setTimeout(() => setOpen(false), 150)}
      onKeyDown={e => {
        if (e.key === 'ArrowDown') { e.preventDefault(); setOpen(true); setHighlight(h => Math.min(h + 1, Math.max(filtered.length - 1, 0))) }
        else if (e.key === 'ArrowUp') { e.preventDefault(); setHighlight(h => Math.max(h - 1, 0)) }
        else if (e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); if (filtered[highlight]) choose(filtered[highlight]); else if (query.trim()) void createFromQuery() }
        else if (e.key === 'Escape') { setOpen(false) }
      }}
    />
    {open && <ul className="category-combobox-list" id={`${id}-list`} role="listbox">
      {filtered.map((category, i) => <li key={category} role="option" aria-selected={category === value}><button type="button" className={i === highlight ? 'is-active' : ''} onMouseDown={e => e.preventDefault()} onClick={() => choose(category)}>
        <span>{category}</span>{recent.includes(category) && category !== value && <span className="tag">Recent</span>}
      </button></li>)}
      {!filtered.length && <li className="category-combobox-empty">No category matches "{query}".</li>}
      {query.trim() && !options.includes(query.trim()) && query.trim().includes(' / ') && <li><button type="button" onMouseDown={e => e.preventDefault()} onClick={() => void createFromQuery()} disabled={creating}>{creating ? 'Creating…' : `Create "${query.trim()}"`}</button></li>}
    </ul>}
    {query.trim() && !options.includes(query.trim()) && !query.trim().includes(' / ') && <small>Type "Group / Name" to create a new category inline.</small>}
  </div>
}

const FIELD_NAMES: FieldName[] = ['category', 'cost_behavior', 'need', 'incoming_type']
const RETIRE_KIND: Record<string, string> = { CATEGORY_RULE_SET: 'CATEGORY_RULE_RETIRE', ROLE_RULE_SET: 'ROLE_RULE_RETIRE', INBOUND_RULE_SET: 'INBOUND_RULE_RETIRE' }

function CandidateReview({ detail, generation, categories, disabled, hasNext, onClose, onNavigate, onSaved, onUndoable }: {
  detail: Detail; generation: string; categories: string[]; disabled: boolean; hasNext: boolean; onClose: () => void; onNavigate: (delta: number) => Promise<void>; onSaved: (status: string) => void | Promise<void>
  onUndoable: (entry: UndoEntry) => void
}) {
  const [scope, setScope] = useState<'one'|'selected'|'rule'>('rule')
  const [anchors, setAnchors] = useState(() => new Set(detail.records.map(row => row.anchor)))
  const [records, setRecords] = useState(detail.records)
  const [showAllRecords, setShowAllRecords] = useState(false)
  const [showShortcuts, setShowShortcuts] = useState(false)
  const [nextCursor, setNextCursor] = useState(detail.next_cursor ?? null)
  const [options, setOptions] = useState<RuleOption[]>([])
  const [option, setOption] = useState(0)
  const [localCategories, setLocalCategories] = useState(categories)
  const [category, setCategory] = useState('')
  const [reason, setReason] = useState('')
  const [reasonEdited, setReasonEdited] = useState(false)
  const [compiled, setCompiled] = useState<CompiledCombinedRule | null>(null)
  const [compiling, setCompiling] = useState(false)
  const [overlapAcknowledged, setOverlapAcknowledged] = useState(false)
  const [error, setError] = useState('')
  const [detailLoading, setDetailLoading] = useState(false)
  const [merging, setMerging] = useState(false)
  // Grocery-trio fields for the "one"/"selected" scopes (Step 5), shared with
  // the single-transaction editor (Step 2) and bulk editor (Step 3) via
  // `previewClassification`/`useBatchCommand` -- one draft/preview/save
  // implementation, not a third copy of it.
  const [costBehavior, setCostBehavior] = useState('')
  const [need, setNeed] = useState('')
  const [incomingType, setIncomingType] = useState('')
  const [clearCategory, setClearCategory] = useState(false)
  const [clearRoles, setClearRoles] = useState(false)
  const [policy, setPolicy] = useState<'fill_missing' | 'replace'>('replace')
  const [preview, setPreview] = useState<ClassificationPreview | null>(null)
  const [previewing, setPreviewing] = useState(false)
  const panelRef = useRef<HTMLDivElement>(null)
  const categoryFieldId = useRef(`category-${detail.candidate.candidate_id}`).current
  /** Undo (Step 5.9) is registered right before a submit resolves, so the
   * closure below always sees the exact `compiled`/`preview`+`reason` that
   * were just saved, not whatever the form has drifted to since. */
  const ruleBatch = useBatchCommand(generation, async status => {
    if (compiled) {
      const savedCompiled = compiled, savedReason = reason.trim(), savedLabel = detail.candidate.label
      onUndoable({ id: crypto.randomUUID(), message: `Saved rule for ${savedLabel}. Undo`, undo: () => undoRuleSave(savedCompiled, savedReason) })
    }
    await onSaved(status)
  })
  const batch = useBatchCommand(generation, async status => {
    if (preview) {
      const savedPreview = preview, savedReason = reason.trim(), savedLabel = detail.candidate.label
      onUndoable({ id: crypto.randomUUID(), message: `Saved changes for ${savedLabel}. Undo`, undo: () => undoGroupSave(savedPreview, savedReason) })
    }
    await onSaved(status)
  })
  async function undoRuleSave(saved: CompiledCombinedRule, savedReason: string) {
    const items = saved.commands.map(command => ({ kind: RETIRE_KIND[command.kind] ?? command.kind, anchors: command.anchors, payload: { reason: `Undo: ${savedReason}` } }))
    await api('batch', { request_id: crypto.randomUUID(), expected_generation: generation, reason: `Undo: ${savedReason}`, items })
    await onSaved('undone')
  }
  /** Restores each changed field to the `before` value the preview captured
   * per anchor, grouping anchors that shared the same before value into one
   * draft. A null `before` clears the field; `clear_roles` clears cost
   * behavior and need together (there is no independent clear for just one),
   * so undoing a need-only change can also blank an untouched cost behavior
   * override -- an accepted imprecision rather than new API surface. There is
   * no clear for incoming type at all, so a change from "unset" cannot be
   * undone; that group is left as saved. */
  async function undoGroupSave(saved: ClassificationPreview, savedReason: string) {
    const groups = new Map<string, { field: FieldName; before: string | null; anchors: string[] }>()
    for (const row of saved.rows) {
      for (const field of FIELD_NAMES) {
        const outcome = row.fields[field]
        if (outcome?.changed) {
          const key = `${field}:${outcome.before === null ? 'null' : outcome.before}`
          const bucket = groups.get(key) ?? { field, before: outcome.before, anchors: [] }
          bucket.anchors.push(row.anchor)
          groups.set(key, bucket)
        }
      }
    }
    const items: ClassificationCommand[] = []
    for (const { field, before, anchors: groupAnchors } of groups.values()) {
      if (!before && field === 'incoming_type') continue
      const draft: ClassificationDraftBody = { generation, anchors: groupAnchors, policy: 'replace', reason: `Undo: ${savedReason}` }
      if (before) draft[field] = { intent: 'set', value: before }
      else if (field === 'category') draft.clear_category = true
      else draft.clear_roles = true
      const groupPreview = await previewClassification(draft)
      items.push(...groupPreview.commands)
    }
    if (items.length) await api('batch', { request_id: crypto.randomUUID(), expected_generation: generation, reason: `Undo: ${savedReason}`, items })
    await onSaved('undone')
  }
  const currentPresetFields: PresetFields = {
    ...(!clearCategory && category ? { category } : {}), ...(!clearRoles && costBehavior ? { cost_behavior: costBehavior } : {}),
    ...(!clearRoles && need ? { need } : {}), ...(incomingType ? { incoming_type: incomingType } : {}),
  }
  function applyPreset(fields: PresetFields) {
    if (fields.category !== undefined) { setClearCategory(false); setCategory(fields.category) }
    if (fields.cost_behavior !== undefined) { setClearRoles(false); setCostBehavior(fields.cost_behavior) }
    if (fields.need !== undefined) { setClearRoles(false); setNeed(fields.need) }
    if (fields.incoming_type !== undefined) setIncomingType(fields.incoming_type)
    resetChecks()
  }
  useEffect(() => {
    api<{ options: RuleOption[] }>('categorization/derive-rule', { generation, anchors: detail.records.map(r => r.anchor) })
      .then(result => { setOptions(result.options); setScope(result.options.length ? 'rule' : 'selected') })
      .catch(e => setError((e as Error).message))
  }, [detail, generation])
  useEffect(() => {
    const field = document.getElementById(categoryFieldId)
    field?.focus()
  }, [categoryFieldId])
  useEffect(() => { if (compiled && category) rememberCategory(category) }, [compiled, category])
  // A definitive 409/422 clears the frozen batch body; re-preview before
  // "Save"/"Save & next" can resubmit against a now-stale preview.
  useEffect(() => { if (batch.error && !batch.submitted) setPreview(null) }, [batch.error, batch.submitted])
  function resetChecks() { setCompiled(null); setOverlapAcknowledged(false); ruleBatch.invalidate(); setPreview(null); batch.invalidate() }
  async function createCategory(name: string) {
    setError('')
    try {
      const requestId = crypto.randomUUID()
      const body = { request_id: requestId, expected_generation: generation, kind: 'CATEGORY_CREATE', anchors: { category: name }, payload: { category: name, reason: `Created inline while categorizing ${detail.candidate.label}` } }
      await api('preview', body)
      await api('commands', body)
      setLocalCategories(current => current.includes(name) ? current : [...current, name])
      return true
    } catch (e) { setError((e as Error).message); return false }
  }
  /** Impact check and rule preview run automatically (Step 5.3): 400ms after
   * any field settles, this compiles the same 1-3 commands sharing one
   * rule_id (Plan Step 3) that the old "Preview rule" button used to require
   * a click for. */
  async function previewRule() {
    if (!options[option] || !hasAnyRuleField || !reason.trim()) return
    setError(''); ruleBatch.invalidate(); setCompiling(true); setOverlapAcknowledged(false)
    try {
      setCompiled(await compileCombinedRule(generation, options[option].match, {
        category: category || null, cost_behavior: costBehavior || null,
        need: need || null, incoming_type: incomingType || null,
      }, reason.trim()))
    } catch (e) { setError((e as Error).message) } finally { setCompiling(false) }
  }
  function checkedRecords() { return records.filter(row => anchors.has(row.anchor)) }
  /** Step 6d: a new rule that would leave rows ambiguous or in direction
   * conflict, or that overlaps an existing rule, must be acknowledged before
   * it can be saved -- the numbers are already computed by the same
   * evaluator `/rules/compile` uses (engine.py `preview_rule_impact`). */
  const categoryImpact = compiled?.impact.category
  const hasOverlapWarning = Boolean(
    categoryImpact && ((categoryImpact.ambiguous ?? 0) > 0 || categoryImpact.direction_conflict > 0 || (categoryImpact.overlaps?.length ?? 0) > 0),
  )
  const hasAnyFieldEdit = Boolean(category || costBehavior || need || incomingType || clearCategory || clearRoles)
  const groupReasonRequired = Boolean(costBehavior || need || incomingType || clearCategory || clearRoles)
  const hasAnyRuleField = Boolean(category || costBehavior || need || incomingType)
  /** The one shared draft/preview/save path for "one" and "selected", applied
   * to exactly one anchor or to the checked set respectively -- the grocery
   * trio (category + pattern + need) together, or incoming type where
   * applicable, previewed and saved as one reviewed batch. */
  async function reviewGroupChanges() {
    const targetAnchors = scope === 'one' ? (onlyTarget ? [onlyTarget.anchor] : []) : [...anchors]
    if (!targetAnchors.length || previewing || !hasAnyFieldEdit || (groupReasonRequired && !reason.trim())) return
    setPreviewing(true); setError('')
    try {
      const draft: ClassificationDraftBody = {
        generation, anchors: targetAnchors,
        category: category ? { intent: 'set', value: category } : undefined,
        cost_behavior: costBehavior ? { intent: 'set', value: costBehavior } : undefined,
        need: need ? { intent: 'set', value: need } : undefined,
        incoming_type: incomingType ? { intent: 'set', value: incomingType } : undefined,
        clear_category: clearCategory, clear_roles: clearRoles,
        policy: scope === 'one' ? 'replace' : policy, reason: reason.trim(),
      }
      setPreview(await previewClassification(draft))
      if (category) rememberCategory(category)
    } catch (e) { setError((e as Error).message) } finally { setPreviewing(false) }
  }
  async function saveGroupChanges() {
    if (!preview) return
    await batch.submit(preview.commands, reason.trim())
  }
  function saveAndNext() {
    if (scope === 'rule') { if (compiled && !ruleBatch.busy && !(hasOverlapWarning && !overlapAcknowledged)) void ruleBatch.submit(compiled.commands, reason.trim()) }
    else if (preview && !batch.busy) void saveGroupChanges()
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
  const onlyChecked = checkedRecords()
  const onlyTarget = onlyChecked.length === 1 ? onlyChecked[0] : null
  const targetRecords = scope === 'one' ? (onlyTarget ? [onlyTarget] : []) : onlyChecked
  const applicableCounts = (['category', 'cost_behavior', 'need', 'incoming_type'] as const).reduce((counts, field) => {
    counts[field] = targetRecords.filter(row => row.applicable_fields?.[field] ?? false).length
    return counts
  }, {} as Record<keyof Applicability, number>)
  const applicableFields = (['category', 'cost_behavior', 'need', 'incoming_type'] as const).reduce((fields, field) => {
    fields[field] = applicableCounts[field] > 0
    return fields
  }, {} as Applicability)
  const outgoingCount = targetRecords.filter(row => row.direction === 'OUT').length
  const incomingCount = targetRecords.filter(row => row.direction === 'IN').length
  const mixedDirections = outgoingCount > 0 && incomingCount > 0
  function selectDirectionOnly(dir: 'IN' | 'OUT') {
    setAnchors(new Set(records.filter(row => row.direction === dir).map(row => row.anchor)))
    resetChecks()
  }
  function chooseScope(next: 'one' | 'selected' | 'rule') {
    setScope(next)
    if (next === 'one') setAnchors(records.length === 1 ? new Set([records[0].anchor]) : new Set())
    resetChecks()
  }
  const notAllLoaded = records.length < detail.candidate.transaction_count
  const ruleAnchors = scope === 'one' ? (onlyTarget ? [onlyTarget.anchor] : []) : [...anchors]
  /** A generated, editable reason (Step 5.4): the user is never blocked
   * waiting to type one, but can still override it before saving. */
  function generatedReason(): string {
    const bits: string[] = []
    if (clearCategory) bits.push('category cleared')
    else if (category) bits.push(`category set to ${category}`)
    if (clearRoles) bits.push('spending pattern/need cleared')
    else {
      if (costBehavior) bits.push(`spending pattern set to ${label('cost_behavior', costBehavior)}`)
      if (need) bits.push(`spending need set to ${label('need', need)}`)
    }
    if (incomingType) bits.push(`incoming type set to ${INCOMING_LABELS[incomingType]}`)
    if (!bits.length) return ''
    return scope === 'rule' ? `Rule for ${detail.candidate.label}: ${bits.join(', ')}` : `${detail.candidate.label}: ${bits.join(', ')} from inbox`
  }
  useEffect(() => {
    if (!reasonEdited) setReason(generatedReason())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [category, costBehavior, need, incomingType, clearCategory, clearRoles, scope, reasonEdited])
  /** Auto-preview (Step 5.3): 400ms after the draft settles, run the same
   * preview the old explicit "Preview"/"Review" buttons required a click
   * for. The action bar then exposes a single Save/Save & next button,
   * enabled once a preview exists. */
  useEffect(() => {
    const timer = setTimeout(() => {
      if (scope === 'rule') { if (hasAnyRuleField && reason.trim()) void previewRule() }
      else if (hasAnyFieldEdit && (!groupReasonRequired || reason.trim())) void reviewGroupChanges()
    }, 400)
    return () => clearTimeout(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope, option, category, costBehavior, need, incomingType, clearCategory, clearRoles, policy, reason, anchors])
  /** Keyboard shortcuts (Step 5.5). Ignored while the target is a form
   * control so typing in the category/reason fields is never hijacked. */
  useEffect(() => {
    function handleKey(e: KeyboardEvent) {
      const target = e.target as HTMLElement | null
      const typing = Boolean(target) && (['INPUT', 'TEXTAREA', 'SELECT'].includes(target!.tagName) || target!.isContentEditable)
      if (typing) return
      if (e.key >= '1' && e.key <= '6') {
        const recent = loadRecentCategories()[Number(e.key) - 1]
        if (recent) { e.preventDefault(); setClearCategory(false); setCategory(recent) }
      } else if (e.key === 'c') { e.preventDefault(); document.getElementById(categoryFieldId)?.focus() }
      else if (e.key === 'b') { e.preventDefault(); setClearRoles(false); setCostBehavior(v => cycle(COST_BEHAVIORS, v)) }
      else if (e.key === 'n') { e.preventDefault(); setClearRoles(false); setNeed(v => cycle(NEEDS, v)) }
      else if (e.key === 'Enter') { e.preventDefault(); saveAndNext() }
      else if (e.key === 'x') { e.preventDefault(); if (hasNext) void onNavigate(1); else onClose() }
      else if (e.key === 'r') { e.preventDefault(); Array.from(panelRef.current?.querySelectorAll('button') ?? []).find(b => b.textContent?.startsWith('Rename '))?.click() }
      else if (e.key === 'm') { e.preventDefault(); if (detail.candidate.kind === 'merchant') setMerging(true) }
      else if (e.key === '?') { e.preventDefault(); setShowShortcuts(s => !s) }
      else if (e.key === 'Escape') { e.preventDefault(); onClose() }
    }
    document.addEventListener('keydown', handleKey)
    return () => document.removeEventListener('keydown', handleKey)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hasNext, detail.candidate.kind, scope, compiled, preview])
  const visibleRecords = showAllRecords ? records : records.slice(0, 5)
  return <div className="categorization-panel candidate-review" role="region" aria-label={`Review ${detail.candidate.label}`} ref={panelRef}>
    <div className="candidate-review-header"><div className="panel-title"><div><p className="eyebrow">REVIEW GROUP</p><h2>{detail.candidate.label}{detail.candidate.kind === 'merchant' && detail.candidate.merchant_key && <>
      <MerchantRenameControl generation={generation} merchantKey={detail.candidate.merchant_key} currentLabel={detail.candidate.label} disabled={disabled} onSaved={onSaved} />
      <Button type="button" variant="ghost" size="sm" disabled={disabled} onClick={() => setMerging(true)}>Same merchant as…</Button>
    </>}</h2><p className="muted-note">{detail.candidate.transaction_count} similar transactions. Choose exactly what should change — a preview refreshes automatically.</p></div>
    <span className="candidate-review-nav"><Button type="button" variant="ghost" size="sm" onClick={() => setShowShortcuts(s => !s)}><HelpCircle size={14}/></Button><Button variant="ghost" size="sm" onClick={onClose}>Close</Button></span>
    </div></div>
    {showShortcuts && <div className="shortcut-sheet">
      <p><kbd>1</kbd>-<kbd>6</kbd> recent category · <kbd>c</kbd> focus category · <kbd>b</kbd>/<kbd>n</kbd> cycle pattern/need · <kbd>Enter</kbd> save &amp; next · <kbd>x</kbd> skip · <kbd>r</kbd> rename · <kbd>m</kbd> same merchant as… · <kbd>Esc</kbd> close · <kbd>?</kbd> this sheet</p>
    </div>}
    {merging && detail.candidate.merchant_key && <MergeMerchantsDialog generation={generation} seedKeys={[detail.candidate.merchant_key]}
      seedDisplayName={detail.candidate.label} disabled={disabled} onClose={() => setMerging(false)}
      onSaved={async status => { setMerging(false); await onSaved(status) }} />}
    {notAllLoaded && <p className="candidate-records-note">Showing {records.length} of {detail.candidate.transaction_count} loaded so far. Selected counts below cover only loaded records, not the full group.</p>}
    <div className="candidate-records">
      {scope === 'selected' && <label key="all"><input type="checkbox" checked={anchors.size === records.length && records.length > 0} onChange={() => setAnchors(current => current.size === records.length ? new Set() : new Set(records.map(r => r.anchor)))}/><span>{anchors.size === records.length ? `Deselect all ${records.length} loaded` : `Select all ${records.length} loaded`}</span><strong/></label>}
      {visibleRecords.map(row => scope === 'selected'
        ? <label key={row.anchor}><input type="checkbox" checked={anchors.has(row.anchor)} onChange={() => { setAnchors(current => { const next = new Set(current); next.has(row.anchor) ? next.delete(row.anchor) : next.add(row.anchor); return next }); resetChecks() }}/><span>{row.date} · {row.account}</span><strong>{row.currency} {row.amount}</strong><small>{row.description || 'No description'}{row.conflicts?.ambiguous ? ' · overlapping rules need review' : ''}{row.conflicts?.direction ? ' · direction conflict needs review' : ''}</small></label>
        : <button type="button" key={row.anchor} className={`candidate-record-row${scope === 'one' && onlyTarget?.anchor === row.anchor ? ' is-active' : ''}`} disabled={scope === 'rule'}
            onClick={() => { if (scope === 'one') { setAnchors(new Set([row.anchor])); resetChecks() } }}>
            <span>{row.date} · {row.account}</span><strong>{row.currency} {row.amount}</strong><small>{row.description || 'No description'}{row.conflicts?.ambiguous ? ' · overlapping rules need review' : ''}{row.conflicts?.direction ? ' · direction conflict needs review' : ''}</small>
          </button>)}
    </div>
    {records.length > 5 && !showAllRecords && <Button variant="ghost" size="sm" onClick={() => setShowAllRecords(true)}>Show all {records.length}</Button>}
    {nextCursor && <Button variant="outline" size="sm" disabled={detailLoading} onClick={() => void loadMoreRecords()}>{detailLoading ? 'Loading records…' : 'Load more records'}</Button>}
    <div className="scope-segmented" role="radiogroup" aria-label="Choose scope">
      <button type="button" role="radio" aria-checked={scope === 'one'} disabled={disabled || Boolean(ruleBatch.submitted) || batch.busy} onClick={() => chooseScope('one')}>
        This one{onlyTarget ? ` — ${onlyTarget.date} · ${money(onlyTarget.amount, onlyTarget.currency)}${onlyTarget.description ? ` · ${onlyTarget.description}` : ''}` : ''}
      </button>
      <button type="button" role="radio" aria-checked={scope === 'selected'} disabled={disabled || Boolean(ruleBatch.submitted) || batch.busy} onClick={() => chooseScope('selected')}>
        Selected ({anchors.size} of {records.length} loaded)
      </button>
      <button type="button" role="radio" aria-checked={scope === 'rule'} disabled={disabled || !options.length || Boolean(ruleBatch.submitted) || batch.busy} onClick={() => chooseScope('rule')}>
        All matching, now and later
      </button>
    </div>
    {scope === 'one' && !onlyTarget && <p className="form-error">Check exactly one transaction above to act on it.</p>}
    {scope === 'rule' && options.length > 1 && <label>Match scope<select value={option} onChange={e => { setOption(Number(e.target.value)); resetChecks() }}>{options.map((item, index) => <option key={item.label} value={index}>{item.label}</option>)}</select></label>}
    {/* One category field, always mounted at the same position regardless of
        scope, so an in-flight derive-rule fetch flipping the default scope
        from "rule" to "selected" (empty options) can never remount it
        mid-input and silently drop a value the user already typed. */}
    <div className="bulk-field">
      <CategoryCombobox id={categoryFieldId} categories={localCategories} value={clearCategory ? '' : category}
        onChange={c => { setClearCategory(false); setCategory(c); resetChecks() }} onCreate={createCategory}
        disabled={disabled || Boolean(ruleBatch.submitted) || batch.busy || clearCategory}/>
      {(scope === 'one' || scope === 'selected') && <label><input type="checkbox" checked={clearCategory} disabled={disabled || batch.busy}
        onChange={e => { setClearCategory(e.target.checked); if (e.target.checked) setCategory(''); resetChecks() }}/>
        {' '}Remove manual category overrides <small>(only affects transactions that have one)</small>
      </label>}
    </div>
    {/* Same trio/incoming fields as the "one"/"selected" scopes, gated by the
        same Step 1 applicability, so "all matching, now and later" is not a
        second, narrower form (Plan Step 3). An empty select here means "no
        rule for this dimension"; saving clears (retires) a dimension that
        previously had one. */}
    {scope === 'rule' && <div className="bulk-form">
      {applicableFields.cost_behavior && <div className="bulk-field">
        <label>Spending pattern
          <select aria-label="Set spending pattern" value={costBehavior} disabled={disabled || ruleBatch.busy}
            onChange={e => { setCostBehavior(e.target.value); resetChecks() }}>
            <option value="">{targetRecords[0]?.effective?.cost_behavior ? `No rule (default: ${label('cost_behavior', targetRecords[0].effective.cost_behavior)})` : 'No rule for this field'}</option>
            {COST_BEHAVIORS.map(v => <option key={v} value={v}>{label('cost_behavior', v)}</option>)}
          </select>
        </label>
      </div>}
      {applicableFields.need && <div className="bulk-field">
        <label>Spending need
          <select aria-label="Set spending need" value={need} disabled={disabled || ruleBatch.busy}
            onChange={e => { setNeed(e.target.value); resetChecks() }}>
            <option value="">{targetRecords[0]?.effective?.need ? `No rule (default: ${label('need', targetRecords[0].effective.need)})` : 'No rule for this field'}</option>
            {NEEDS.map(v => <option key={v} value={v}>{label('need', v)}</option>)}
          </select>
        </label>
      </div>}
      {applicableFields.incoming_type && <div className="bulk-field">
        <label>Incoming type
          <select aria-label="Set incoming type" value={incomingType} disabled={disabled || ruleBatch.busy}
            onChange={e => { setIncomingType(e.target.value); resetChecks() }}>
            <option value="">No rule for this field</option>
            {INCOMING_TYPES.map(v => <option key={v} value={v}>{INCOMING_LABELS[v]}</option>)}
          </select>
        </label>
      </div>}
    </div>}
    {scope === 'rule' && <label>Reason<input value={reason} onChange={e => { setReason(e.target.value); setReasonEdited(true); resetChecks() }} /></label>}
    {scope === 'selected' && mixedDirections && <p className="muted-note">{outgoingCount} outgoing · {incomingCount} incoming
      <Button type="button" variant="ghost" size="sm" onClick={() => selectDirectionOnly('OUT')}>Select outgoing only</Button>
      <Button type="button" variant="ghost" size="sm" onClick={() => selectDirectionOnly('IN')}>Select incoming only</Button>
    </p>}
    {(scope === 'one' || scope === 'selected') && <PresetPicker applicable={applicableFields} categories={localCategories}
      current={currentPresetFields} onApply={applyPreset} disabled={disabled || Boolean(ruleBatch.submitted) || batch.busy}/>}
    {(scope === 'one' || scope === 'selected') && <ClassificationFieldsFieldset
      hideCategory
      categoryId={categoryFieldId} categories={localCategories} disabled={disabled} busy={batch.busy}
      category={category} onCategory={c => { setCategory(c); resetChecks() }}
      clearCategory={clearCategory} onClearCategory={v => { setClearCategory(v); resetChecks() }}
      costBehavior={costBehavior} onCostBehavior={v => { setCostBehavior(v); resetChecks() }}
      need={need} onNeed={v => { setNeed(v); resetChecks() }}
      clearRoles={clearRoles} onClearRoles={v => { setClearRoles(v); resetChecks() }}
      incomingType={incomingType} onIncomingType={v => { setIncomingType(v); resetChecks() }}
      policy={policy} onPolicy={v => { setPolicy(v); resetChecks() }} showPolicy={scope === 'selected'}
      reason={reason} onReason={v => { setReason(v); setReasonEdited(true); resetChecks() }} reasonRequired={groupReasonRequired}
      applicable={applicableFields}
    />}
    {(scope === 'one' || scope === 'selected') && targetRecords.length > 0 && <p className="muted-note">Field applicability: {(['category', 'cost_behavior', 'need', 'incoming_type'] as const).filter(field => applicableFields[field]).map(field => `${field.replaceAll('_', ' ')} applies to ${applicableCounts[field]} of ${targetRecords.length}`).join('; ') || 'no classification fields apply to this selection'}.</p>}
    {(scope === 'one' || scope === 'selected') && ruleAnchors.length > 0 && <RuleSection generation={generation} anchors={ruleAnchors}
      category={clearCategory ? '' : category} costBehavior={clearRoles ? '' : costBehavior} need={clearRoles ? '' : need}
      reason={reason} disabled={disabled} onSaved={onSaved}/>}
    {error && <p className="form-error">{error}</p>}{ruleBatch.error && <p className="form-error">{ruleBatch.error}</p>}{batch.error && <p className="form-error">{batch.error}</p>}
    {scope === 'rule' && compiled && !ruleBatch.submitted && <CombinedImpactSummary impact={compiled.impact}/>}
    {scope === 'rule' && hasOverlapWarning && !ruleBatch.submitted && <label className="form-error">
      <input type="checkbox" checked={overlapAcknowledged} onChange={e => setOverlapAcknowledged(e.target.checked)}/>
      {' '}I understand this rule leaves some transactions ambiguous, in direction conflict, or overlapping another rule.
    </label>}
    {(scope === 'one' || scope === 'selected') && preview && <ClassificationPreviewSummary preview={preview}/>}
    {batch.submitted && batch.error && <p className="muted-note">The outcome may be unknown. Retry sends the same request safely; do not create a second decision.</p>}
    <p className="keyboard-hint"><kbd>Esc</kbd> close · <kbd>Enter</kbd> save &amp; next · <kbd>?</kbd> all shortcuts.</p>
    <div className="dialog-actions">
      <Button type="button" variant="outline" onClick={() => void onNavigate(-1)}>Previous</Button>
      <Button type="button" variant="outline" onClick={() => hasNext ? void onNavigate(1) : onClose()}>Skip</Button>
      {scope === 'rule'
        ? <Button disabled={disabled || !compiled || ruleBatch.busy || (hasOverlapWarning && !overlapAcknowledged)} onClick={saveAndNext}>
            {ruleBatch.busy ? (ruleBatch.submitted && ruleBatch.error ? 'Retrying…' : 'Saving…') : compiled ? (hasNext ? 'Save & next' : 'Save') : (compiling ? 'Checking…' : 'Waiting for preview…')}
          </Button>
        : <Button disabled={disabled || !preview || batch.busy} onClick={saveAndNext}>
            {batch.busy ? (batch.submitted && batch.error ? 'Retrying…' : 'Saving…') : preview ? (hasNext ? 'Save & next' : 'Save') : (previewing ? 'Checking…' : 'Waiting for preview…')}
          </Button>}
    </div>
  </div>
}
