import { useCallback, useEffect, useRef, useState } from 'react'
import { ChevronRight, RefreshCw, Tag } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { api, money } from '@/lib/api'
import { useBatchCommand } from '@/lib/command'
import { compileCombinedRule, type CompiledCombinedRule, type CompiledRule } from '@/features/rules/api'
import { CombinedImpactSummary, MerchantRenameControl } from '@/features/rules'
import { rememberCategory as rememberSharedCategory } from '@/features/classification/CategoryPicker'
import { ClassificationFieldsFieldset } from '@/features/classification/FieldsFieldset'
import { ClassificationPreviewSummary } from '@/features/classification/PreviewSummary'
import { PresetPicker } from '@/features/classification/PresetPicker'
import { RuleSection } from '@/features/classification/RuleSection'
import type { PresetFields } from '@/features/classification/presets'
import { previewClassification, COST_BEHAVIORS, NEEDS, INCOMING_TYPES, INCOMING_LABELS, label, type Applicability, type ClassificationDraftBody, type ClassificationPreview } from '@/features/classification/api'

type Summary = {
  counts: Record<string, number>; active_categorization_denominator: number; progress_numerator: number
  needs_attention: number; combined_completeness?: { numerator: number; denominator: number }
}
type Candidate = { candidate_id: string; kind: string; label: string; merchant_key?: string; transaction_count: number; active_count: number; date_range: { first: string; last: string }; accounts: string[]; directions: Record<string, number>; examples: string[] }
type CandidatesPage = { candidates: Candidate[]; next_cursor: string | null; total: number }
type CandidateRecord = { anchor: string; description: string | null; account: string; currency: string; amount: string; date: string; direction: string; active: boolean; merchant_raw?: string | null; merchant_normalized?: string | null; detail_bucket?: string; applicable_fields?: Applicability; effective?: Record<string, string | null>; conflicts?: { ambiguous: boolean; direction: boolean } }
type Detail = { candidate: Candidate; records: CandidateRecord[]; next_cursor?: string | null }
type RuleOption = { label: string; match: Record<string, unknown> }

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

const RECENT_CATEGORIES_KEY = 'fincl.categorization.recentCategories'

function loadRecentCategories(): string[] {
  try { return JSON.parse(localStorage.getItem(RECENT_CATEGORIES_KEY) || '[]') } catch { return [] }
}
function rememberCategory(category: string) {
  try {
    const next = [category, ...loadRecentCategories().filter(c => c !== category)].slice(0, 6)
    localStorage.setItem(RECENT_CATEGORIES_KEY, JSON.stringify(next))
  } catch { /* private browsing or storage disabled */ }
}

export function CategorizationView({ generation, freshness, categories, accounts = [], currencies = [], onSaved }: {
  generation: string; freshness: string; categories: string[]; accounts?: string[]; currencies?: string[]; onSaved: (status: string) => void | Promise<void>
}) {
  const [summary, setSummary] = useState<Summary | null>(null)
  const [page, setPage] = useState<CandidatesPage | null>(null)
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
  const filters = new URLSearchParams({ direction, lifecycle, kind, account, currency, focus })
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
    <div className="scope-caption"><span>Group similar unclassified transactions, then make a deliberate one-off or reusable decision. <kbd>j</kbd>/<kbd>k</kbd> to move, <kbd>Enter</kbd> to open.</span><span className="categorization-filters"><select aria-label="Filter categorization focus" value={focus} onChange={e => setFocus(e.target.value)}>{FOCUSES.map(f => <option key={f.value} value={f.value}>{f.label}</option>)}</select><select aria-label="Filter categorization candidate kind" value={kind} onChange={e => setKind(e.target.value)}><option value="">All group types</option><option value="merchant">Merchant</option><option value="paynow_recipient">PayNow recipient</option><option value="description">Description</option><option value="missing_merchant">Missing merchant</option></select><select aria-label="Filter categorization direction" value={direction} onChange={e => setDirection(e.target.value)}><option value="">All directions</option><option value="OUT">Outgoing</option><option value="IN">Incoming</option></select><select aria-label="Filter categorization lifecycle" value={lifecycle} onChange={e => setLifecycle(e.target.value)}><option value="">All lifecycle states</option><option value="COMPLETED">Active only</option><option value="PENDING">Pending</option><option value="REVERTED">Reverted</option></select><select aria-label="Filter categorization account" value={account} onChange={e => setAccount(e.target.value)}><option value="">All accounts</option>{accounts.map(a => <option key={a}>{a}</option>)}</select><select aria-label="Filter categorization currency" value={currency} onChange={e => setCurrency(e.target.value)}><option value="">All currencies</option>{currencies.map(c => <option key={c}>{c}</option>)}</select><Button variant="ghost" size="sm" onClick={() => void load()}><RefreshCw size={14}/>Refresh</Button></span></div>
    {summary && <CoverageSummary summary={summary}/>}
    <Card className="candidate-list" onKeyDown={onListKeyDown}>
      <div className="panel-title"><div><p className="eyebrow"><span>CATEGORIZATION INBOX</span> · ALL SELECTED HISTORY</p><h2>{page ? (focus === 'category' ? `${page.total} groups need review` : `${page.total} groups match ${FOCUSES.find(f => f.value === focus)?.label.toLowerCase()}`) : 'Loading groups…'}</h2><p className="muted-note">Covers every imported statement, all dates and currencies. Rules also apply to future imports. Pending, reverted and superseded rows are shown but not counted as active.</p></div></div>
      {!page?.candidates.length && <p className="empty-line">{focus === 'category' ? 'Everything active is categorized, or no groups match the current ledger generation.' : 'Nothing active matches this focus, or no groups match the current ledger generation.'}</p>}
      {page?.candidates.map((candidate, index) => <button className={`candidate-card${index === activeIndex ? ' is-active' : ''}`} key={candidate.candidate_id} tabIndex={index === activeIndex ? 0 : -1} onFocus={() => setActiveIndex(index)} onClick={() => void open(candidate, index)}>
        <span className="merchant-icon"><Tag size={16}/></span><span className="candidate-main"><strong>{candidate.label}</strong><small>{candidate.transaction_count} transactions · {candidate.active_count} active · {candidate.date_range.first} to {candidate.date_range.last}</small><small>{candidate.examples.slice(0, 2).join(' · ') || candidate.kind.replace('_', ' ')}</small>{candidate.directions.IN > 0 && candidate.directions.OUT > 0 && <small className="tag">includes {candidate.directions.IN} incoming</small>}</span><ChevronRight size={16}/>
      </button>)}
      {page?.next_cursor && <div className="dialog-actions"><Button variant="outline" onClick={() => void loadMoreCandidates()}>Load more groups</Button></div>}
    </Card>
    {selected && <CandidateReview
      key={selected.candidate.candidate_id}
      detail={selected} generation={generation} categories={categories} disabled={disabled}
      hasNext={selectedIndex !== null && selectedIndex + 1 < queue.length}
      onClose={() => { setSelected(null); setSelectedIndex(null) }}
      onNavigate={async delta => { if (selectedIndex !== null && queue[selectedIndex + delta]) await open(queue[selectedIndex + delta], selectedIndex + delta) }}
      onSaved={afterSave}
    />}
  </div>
}

function CoverageSummary({ summary }: { summary: Summary }) {
  const percent = summary.active_categorization_denominator ? Math.round(summary.progress_numerator / summary.active_categorization_denominator * 100) : 100
  const combined = summary.combined_completeness
  const combinedPercent = combined?.denominator ? Math.round(combined.numerator / combined.denominator * 100) : 100
  return <div className="coverage-summary"><Card><small>Rule-assigned</small><strong>{summary.counts.rule_assigned || 0}</strong></Card><Card><small>Manually assigned</small><strong>{summary.counts.manual_override || 0}</strong></Card><Card><small>Needs a category</small><strong>{summary.counts.unclassified_active || 0}</strong></Card><Card><small>Category progress</small><strong>{percent}%</strong><small>{summary.progress_numerator} / {summary.active_categorization_denominator}</small></Card>{combined && <Card><small>Combined completeness</small><strong>{combinedPercent}%</strong><small>{combined.numerator} / {combined.denominator} — category, spending pattern/need, and incoming type where each applies</small></Card>}{summary.needs_attention > 0 && <Card><small>Needs attention</small><strong>{summary.needs_attention}</strong></Card>}</div>
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
        else if (e.key === 'Enter') { e.preventDefault(); if (filtered[highlight]) choose(filtered[highlight]); else if (query.trim()) void createFromQuery() }
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

function CandidateReview({ detail, generation, categories, disabled, hasNext, onClose, onNavigate, onSaved }: {
  detail: Detail; generation: string; categories: string[]; disabled: boolean; hasNext: boolean; onClose: () => void; onNavigate: (delta: number) => Promise<void>; onSaved: (status: string) => void | Promise<void>
}) {
  const [scope, setScope] = useState<'one'|'selected'|'rule'>('rule')
  const [anchors, setAnchors] = useState(() => new Set(detail.records.map(row => row.anchor)))
  const [records, setRecords] = useState(detail.records)
  const [nextCursor, setNextCursor] = useState(detail.next_cursor ?? null)
  const [options, setOptions] = useState<RuleOption[]>([])
  const [option, setOption] = useState(0)
  const [localCategories, setLocalCategories] = useState(categories)
  const [category, setCategory] = useState('')
  const [reason, setReason] = useState('')
  const [compiled, setCompiled] = useState<CompiledCombinedRule | null>(null)
  const [categoryImpactOverride, setCategoryImpactOverride] = useState<CompiledRule['impact'] | null>(null)
  const [error, setError] = useState('')
  const [batching, setBatching] = useState(false)
  const [detailLoading, setDetailLoading] = useState(false)
  // Grocery-trio fields for the "one"/"selected" scopes (Step 5), shared with
  // the single-transaction editor (Step 2) and bulk editor (Step 3) via
  // `previewClassification`/`useBatchCommand` -- one draft/preview/save
  // implementation, not a third copy of it. The "rule" scope below keeps its
  // own existing category+alias flow untouched, per the plan's instruction
  // to preserve existing rule/alias capabilities in this step.
  const [costBehavior, setCostBehavior] = useState('')
  const [need, setNeed] = useState('')
  const [incomingType, setIncomingType] = useState('')
  const [clearCategory, setClearCategory] = useState(false)
  const [clearRoles, setClearRoles] = useState(false)
  const [policy, setPolicy] = useState<'fill_missing' | 'replace'>('replace')
  const [preview, setPreview] = useState<ClassificationPreview | null>(null)
  const [previewing, setPreviewing] = useState(false)
  const variants = [...new Set(detail.records.map(row => row.merchant_normalized).filter((value): value is string => Boolean(value)))]
  const [aliasVariants, setAliasVariants] = useState<Set<string>>(() => new Set())
  // The alias's canonical is always the group's own key -- never a value the
  // user can retype -- so a rule compiled against that key (below) can never
  // drift from what the alias actually resolves to after saving (Plan Step 2,
  // fixing F2.1/F2.3). Only the *display* name is user-editable here.
  const merchantKey = detail.candidate.merchant_key ?? variants[0] ?? detail.candidate.label
  const [aliasDisplayName, setAliasDisplayName] = useState(detail.candidate.label)
  const ruleBatch = useBatchCommand(generation, onSaved) // "rule" scope only
  const batch = useBatchCommand(generation, onSaved) // "one"/"selected" scopes only
  const categoryFieldId = useRef(`category-${detail.candidate.candidate_id}`).current
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
      .then(result => { setOptions(result.options); if (!result.options.length) setScope('one') })
      .catch(e => setError((e as Error).message))
  }, [detail, generation])
  useEffect(() => {
    const field = document.getElementById(categoryFieldId)
    field?.focus()
  }, [categoryFieldId])
  useEffect(() => { if (compiled && category) rememberCategory(category) }, [compiled])
  // A definitive 409/422 clears the frozen batch body; re-preview before
  // "Save changes"/"Save and next" can resubmit against a now-stale preview.
  useEffect(() => { if (batch.error && !batch.submitted) setPreview(null) }, [batch.error, batch.submitted])
  function resetChecks() { setCompiled(null); setCategoryImpactOverride(null); ruleBatch.invalidate(); setPreview(null); batch.invalidate() }
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
  /** Impact check and rule preview are the same click: breadth no longer buys a shortcut past confirmation.
   * One rule save can carry category, spending pattern/need and incoming type together, compiled server-side
   * into 1-3 commands sharing one rule_id (Plan Step 3). */
  async function previewRule() {
    if (!options[option] || !hasAnyRuleField || !reason.trim()) return
    setError(''); setCategoryImpactOverride(null); ruleBatch.invalidate()
    try {
      if (aliasVariants.size && category) {
        // Preview against the key the alias will actually carry after saving
        // (merchantKey, never a user-retyped value) so the projected match
        // count reflects the rule that is about to be saved, not a
        // hypothetical different canonical (Plan Step 2, F2.1).
        const preview = await api<{ impact: CompiledRule['impact'] }>('categorization/impact', {
          generation, match: options[option].match, category, reason: reason.trim(),
          alias_canonical: merchantKey, alias_patterns: [...aliasVariants].map(exact => ({ exact })),
        })
        setCategoryImpactOverride(preview.impact)
      }
      setCompiled(await compileCombinedRule(generation, options[option].match, {
        category: category || null, cost_behavior: costBehavior || null,
        need: need || null, incoming_type: incomingType || null,
      }, reason.trim()))
    } catch (e) { setError((e as Error).message) }
  }
  function checkedRecords() { return records.filter(row => anchors.has(row.anchor)) }
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
      if (category) rememberSharedCategory(category)
    } catch (e) { setError((e as Error).message) } finally { setPreviewing(false) }
  }
  async function saveGroupChanges() {
    if (!preview) return
    await batch.submit(preview.commands, reason.trim())
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
          { kind: 'MERCHANT_ALIAS_SET', anchors: { canonical: merchantKey }, payload: { patterns: [...aliasVariants].map(exact => ({ exact })), display_name: aliasDisplayName.trim(), reason: reasonText } },
          ...compiled.commands,
        ],
      })
      if (category) rememberCategory(category)
      await onSaved(result.status)
    } catch (e) { setError((e as Error).message) } finally { setBatching(false) }
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
  const notAllLoaded = records.length < detail.candidate.transaction_count
  const ruleAnchors = scope === 'one' ? (onlyTarget ? [onlyTarget.anchor] : []) : [...anchors]
  return <Dialog open onOpenChange={open => { if (!open) onClose() }}>
    <DialogContent className="candidate-review candidate-review-dialog" showCloseButton={false} aria-label={`Review ${detail.candidate.label}`} onKeyDown={e => { if (e.key === 'Escape') onClose() }}>
    <DialogHeader className="candidate-review-header"><div className="panel-title"><div><p className="eyebrow">REVIEW GROUP</p><DialogTitle>{detail.candidate.label}{detail.candidate.kind === 'merchant' && detail.candidate.merchant_key && <MerchantRenameControl generation={generation} merchantKey={detail.candidate.merchant_key} currentLabel={detail.candidate.label} disabled={disabled} onSaved={onSaved} />}</DialogTitle><DialogDescription>{detail.candidate.transaction_count} similar transactions. Choose exactly what should change before saving.</DialogDescription></div><Button variant="ghost" size="sm" onClick={onClose}>Close</Button></div></DialogHeader>
    {notAllLoaded && <p className="candidate-records-note">Showing {records.length} of {detail.candidate.transaction_count} loaded so far. Selected counts below cover only loaded records, not the full group.</p>}
    <div className="candidate-records">
      <label key="all"><input type="checkbox" checked={anchors.size === records.length && records.length > 0} onChange={() => setAnchors(current => current.size === records.length ? new Set() : new Set(records.map(r => r.anchor)))}/><span>{anchors.size === records.length ? `Deselect all ${records.length} loaded` : `Select all ${records.length} loaded`}</span><strong/></label>
      {records.map(row => <label key={row.anchor}><input type="checkbox" checked={anchors.has(row.anchor)} onChange={() => { setAnchors(current => { const next = new Set(current); next.has(row.anchor) ? next.delete(row.anchor) : next.add(row.anchor); return next }); resetChecks() }}/><span>{row.date} · {row.account}</span><strong>{row.currency} {row.amount}</strong><small>{row.description || 'No description'}{row.conflicts?.ambiguous ? ' · overlapping rules need review' : ''}{row.conflicts?.direction ? ' · direction conflict needs review' : ''}</small></label>)}
    </div>
    {nextCursor && <Button variant="outline" size="sm" disabled={detailLoading} onClick={() => void loadMoreRecords()}>{detailLoading ? 'Loading records…' : 'Load more records'}</Button>}
    <fieldset disabled={disabled || Boolean(ruleBatch.submitted) || batching || batch.busy}><legend>Choose scope</legend>
      <label><input type="radio" checked={scope === 'one'} onChange={() => { setScope('one'); resetChecks() }}/> Only this transaction{onlyTarget ? ` — ${onlyTarget.date} · ${money(onlyTarget.amount, onlyTarget.currency)}${onlyTarget.description ? ` · ${onlyTarget.description}` : ''}` : ' (check exactly one below)'}</label>
      <label><input type="radio" checked={scope === 'selected'} onChange={() => { setScope('selected'); resetChecks() }}/> Selected transactions ({anchors.size} of {records.length} loaded)</label>
      <label><input type="radio" checked={scope === 'rule'} disabled={!options.length} onChange={() => { setScope('rule'); resetChecks() }}/> All matching transactions, now and later</label>
    </fieldset>
    {scope === 'one' && !onlyTarget && <p className="form-error">Check exactly one transaction above to act on it.</p>}
    {scope === 'rule' && options.length > 1 && <label>Match scope<select value={option} onChange={e => { setOption(Number(e.target.value)); resetChecks() }}>{options.map((item, index) => <option key={item.label} value={index}>{item.label}</option>)}</select></label>}
    {/* TODO(step-4): replace with merge */}
    {scope === 'rule' && variants.length > 0 && <fieldset className="alias-step"><legend>Normalize statement-name variants <small>(optional)</small></legend>
      <label>Display name<input value={aliasDisplayName} onChange={e => setAliasDisplayName(e.target.value)} /></label>
      <label>Merchant key <small>(what rules match against — never changes here)</small><input value={merchantKey} readOnly disabled /></label>
      {variants.map(variant => <label key={variant}><input type="checkbox" checked={aliasVariants.has(variant)} onChange={() => { setAliasVariants(current => { const next = new Set(current); next.has(variant) ? next.delete(variant) : next.add(variant); return next }); resetChecks() }}/>{variant}</label>)}
    </fieldset>}
    {/* One category field, always mounted at the same position regardless of
        scope, so an in-flight derive-rule fetch flipping the default scope
        from "rule" to "one" (empty options) can never remount it mid-input
        and silently drop a value the user already typed. */}
    <div className="bulk-field">
      <CategoryCombobox id={categoryFieldId} categories={localCategories} value={clearCategory ? '' : category}
        onChange={c => { setClearCategory(false); setCategory(c); resetChecks() }} onCreate={createCategory}
        disabled={disabled || Boolean(ruleBatch.submitted) || batching || batch.busy || clearCategory}/>
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
          <select aria-label="Set spending pattern" value={costBehavior} disabled={disabled || batching || ruleBatch.busy}
            onChange={e => { setCostBehavior(e.target.value); resetChecks() }}>
            <option value="">{targetRecords[0]?.effective?.cost_behavior ? `No rule (default: ${label('cost_behavior', targetRecords[0].effective.cost_behavior)})` : 'No rule for this field'}</option>
            {COST_BEHAVIORS.map(v => <option key={v} value={v}>{label('cost_behavior', v)}</option>)}
          </select>
        </label>
      </div>}
      {applicableFields.need && <div className="bulk-field">
        <label>Spending need
          <select aria-label="Set spending need" value={need} disabled={disabled || batching || ruleBatch.busy}
            onChange={e => { setNeed(e.target.value); resetChecks() }}>
            <option value="">{targetRecords[0]?.effective?.need ? `No rule (default: ${label('need', targetRecords[0].effective.need)})` : 'No rule for this field'}</option>
            {NEEDS.map(v => <option key={v} value={v}>{label('need', v)}</option>)}
          </select>
        </label>
      </div>}
      {applicableFields.incoming_type && <div className="bulk-field">
        <label>Incoming type
          <select aria-label="Set incoming type" value={incomingType} disabled={disabled || batching || ruleBatch.busy}
            onChange={e => { setIncomingType(e.target.value); resetChecks() }}>
            <option value="">No rule for this field</option>
            {INCOMING_TYPES.map(v => <option key={v} value={v}>{INCOMING_LABELS[v]}</option>)}
          </select>
        </label>
      </div>}
    </div>}
    {scope === 'rule' && <label>Reason<input value={reason} onChange={e => { setReason(e.target.value); resetChecks() }} /></label>}
    {scope === 'selected' && mixedDirections && <p className="muted-note">{outgoingCount} outgoing · {incomingCount} incoming
      <Button type="button" variant="ghost" size="sm" onClick={() => selectDirectionOnly('OUT')}>Select outgoing only</Button>
      <Button type="button" variant="ghost" size="sm" onClick={() => selectDirectionOnly('IN')}>Select incoming only</Button>
    </p>}
    {(scope === 'one' || scope === 'selected') && <PresetPicker applicable={applicableFields} categories={localCategories}
      current={currentPresetFields} onApply={applyPreset} disabled={disabled || Boolean(ruleBatch.submitted) || batching || batch.busy}/>}
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
      reason={reason} onReason={v => { setReason(v); resetChecks() }} reasonRequired={groupReasonRequired}
      applicable={applicableFields}
    />}
    {(scope === 'one' || scope === 'selected') && targetRecords.length > 0 && <p className="muted-note">Field applicability: {(['category', 'cost_behavior', 'need', 'incoming_type'] as const).filter(field => applicableFields[field]).map(field => `${field.replaceAll('_', ' ')} applies to ${applicableCounts[field]} of ${targetRecords.length}`).join('; ') || 'no classification fields apply to this selection'}.</p>}
    {(scope === 'one' || scope === 'selected') && ruleAnchors.length > 0 && <RuleSection generation={generation} anchors={ruleAnchors}
      category={clearCategory ? '' : category} costBehavior={clearRoles ? '' : costBehavior} need={clearRoles ? '' : need}
      reason={reason} disabled={disabled} onSaved={onSaved}/>}
    {error && <p className="form-error">{error}</p>}{ruleBatch.error && <p className="form-error">{ruleBatch.error}</p>}{batch.error && <p className="form-error">{batch.error}</p>}
    {scope === 'rule' && compiled && !ruleBatch.submitted && <CombinedImpactSummary impact={compiled.impact} categoryOverride={categoryImpactOverride}/>}
    {(scope === 'one' || scope === 'selected') && preview && <ClassificationPreviewSummary preview={preview}/>}
    {batch.submitted && batch.error && <p className="muted-note">The outcome may be unknown. Retry sends the same request safely; do not create a second decision.</p>}
    <p className="keyboard-hint"><kbd>Esc</kbd> to close without saving.</p>
    <div className="dialog-actions">
      <Button type="button" variant="outline" onClick={() => void onNavigate(-1)}>Previous</Button>
      <Button type="button" variant="outline" onClick={() => hasNext ? void onNavigate(1) : onClose()}>Skip</Button>
      {scope === 'one' ? <Button
          disabled={disabled || previewing || batch.busy || !onlyTarget || (!preview && (!hasAnyFieldEdit || (groupReasonRequired && !reason.trim())))}
          onClick={() => { if (preview) void saveGroupChanges(); else void reviewGroupChanges() }}>
          {previewing ? 'Checking…' : batch.busy ? (batch.submitted && batch.error ? 'Retrying…' : 'Saving…') : preview ? (hasNext ? 'Save and next' : 'Save changes') : 'Review changes'}
        </Button>
      : scope === 'selected' ? <Button
          disabled={disabled || previewing || batch.busy || !anchors.size || (!preview && (!hasAnyFieldEdit || (groupReasonRequired && !reason.trim())))}
          onClick={() => { if (preview) void saveGroupChanges(); else void reviewGroupChanges() }}>
          {previewing ? 'Checking…' : batch.busy ? (batch.submitted && batch.error ? 'Retrying…' : 'Saving…') : preview ? (hasNext ? 'Save and next' : `Save ${anchors.size} selected transactions`) : `Review ${anchors.size} selected transaction${anchors.size === 1 ? '' : 's'}`}
        </Button>
      : aliasVariants.size ? <Button disabled={disabled || !compiled || !aliasDisplayName.trim() || batching} onClick={() => void saveRuleWithAlias()}>{batching ? 'Saving alias and rule…' : 'Save alias and rule together'}</Button>
      : <Button disabled={disabled || !hasAnyRuleField || !reason.trim() || ruleBatch.busy} onClick={() => { if (compiled) void ruleBatch.submit(compiled.commands, reason.trim()); else void previewRule() }}>{ruleBatch.busy ? (ruleBatch.submitted && ruleBatch.error ? 'Retrying…' : 'Saving…') : compiled ? (hasNext ? 'Save and next' : 'Save rule') : 'Preview rule'}</Button>}
    </div>
    </DialogContent>
  </Dialog>
}
