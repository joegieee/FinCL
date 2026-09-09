import { FormEvent, useCallback, useEffect, useRef, useState } from 'react'
import { AlertTriangle, CalendarClock, CheckCircle2, RefreshCw, SlidersHorizontal } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { money, shortCategory } from '@/lib/api'
import { loadSpendingControls, runSpendingWhatIf, type SpendingControlsPage, type WhatIfDraft } from './api'

type Mode = 'cut' | 'date' | 'income'
const groupLabels = {
  mandatory_lifestyle: 'Mandatory lifestyle',
  debts_and_cards: 'Debts & cards',
  important_commitments: 'Important commitments',
  planned_allocations: 'Planned allocations',
}

export function ControlsView({ initialMonth, currency }: { initialMonth: string; currency: string }) {
  const [month, setMonth] = useState(initialMonth)
  const [page, setPage] = useState<SpendingControlsPage | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const seq = useRef(0)
  const load = useCallback((selected = month) => {
    const id = ++seq.current
    setLoading(true); setError('')
    loadSpendingControls(selected, currency)
      .then(value => { if (id === seq.current) setPage(value) })
      .catch(reason => { if (id === seq.current) setError((reason as Error).message) })
      .finally(() => { if (id === seq.current) setLoading(false) })
  }, [currency, month])
  useEffect(() => { setMonth(initialMonth); load(initialMonth) }, [currency, initialMonth])

  if (loading && !page) return <div className="skeleton-page" aria-label="Loading spending controls"><div /><div /><div /></div>
  if (error && !page) return <div className="alert error" role="alert"><div><strong>We couldn’t load spending controls.</strong><p>{error}</p></div><Button variant="outline" onClick={() => load()}>Try again</Button></div>
  if (!page) return null

  return <div className="controls-view">
    <div className="scope-caption">
      <span>{page.horizon ? `Conservative path ${page.horizon.supported_cash_cutoff} → ${page.horizon.end}` : 'No supported forecast horizon'} · proposals never change recorded totals</span>
      <div className="controls-month"><label>Budget month <Input type="month" value={month} onChange={event => setMonth(event.target.value)} /></label><Button variant="outline" onClick={() => load()} disabled={loading}><RefreshCw size={14} className={loading ? 'spin' : ''} /> Apply</Button></div>
    </div>
    {error && <div className="alert error" role="alert"><div><strong>The scenario could not be recomputed.</strong><p>{error}</p></div></div>}
    {!page.available && <div className="alert" role="status"><AlertTriangle size={18} /><div><strong>Controls remain unresolved</strong><p>{page.reason}</p></div></div>}
    <PacePanel page={page} />
    {page.commitments && <CommitmentsPanel page={page} />}
    {page.capacity && <CapacityPanel page={page} />}
    {page.first_breach_drivers && <BreachDrivers page={page} />}
    <WhatIfForm page={page} onResult={setPage} onError={setError} />
    {page.unresolved && page.unresolved.length > 0 && <Card className="controls-card"><div className="panel-title"><div><p className="eyebrow">STILL UNRESOLVED</p><h2>Evidence needed before a definitive allowance</h2></div></div><ul className="issue-list">{page.unresolved.map((item, index) => <li key={`${item}:${index}`}>{item}</li>)}</ul></Card>}
  </div>
}

function PacePanel({ page }: { page: SpendingControlsPage }) {
  return <Card className="controls-card"><div className="panel-title"><div><p className="eyebrow">BUDGET PACE · {page.month}</p><h2>Spending versus this month’s calendar</h2></div></div>
    {page.pace.budgets.length === 0 ? <p className="muted-note">No active {page.currency} budget applies to this month.</p> : <div className="pace-grid">{page.pace.budgets.map(budget => <article key={budget.plan}>
      <div className="pace-head"><div><strong>{budget.name}</strong><small>{budget.category_key ? shortCategory(budget.category_key) : 'All spending + fees'}</small></div><Badge variant="outline" className={budget.coverage.supported ? '' : 'review-badge'}>{budget.coverage.badge}</Badge></div>
      <div className="pace-bars"><div><span>Budget consumed</span><b>{budget.budget_consumed_percent}%</b></div><progress max="100" value={Math.min(100, Number(budget.budget_consumed_percent))} /><div><span>Calendar elapsed</span><b>{budget.calendar_elapsed_percent}%</b></div><progress max="100" value={Number(budget.calendar_elapsed_percent)} /></div>
      <p>{money(budget.actual, page.currency)} observed of {money(budget.limit, page.currency)}</p>
      <small>{budget.pace_projected_spend === null ? 'Pace estimate withheld until coverage supports spending-to-date.' : `Pace estimate: ${money(budget.pace_projected_spend, page.currency)} for the month · estimated headroom ${money(budget.pace_estimated_headroom, page.currency)}.`}</small>
      <small>Strict remaining: {budget.remaining === null ? 'unavailable for this incomplete month' : money(budget.remaining, page.currency)}. This field is unchanged.</small>
    </article>)}</div>}
  </Card>
}

function CommitmentsPanel({ page }: { page: SpendingControlsPage }) {
  const commitments = page.commitments!
  return <Card className="controls-card" id="commitments"><div className="panel-title"><div><p className="eyebrow">DISJOINT COMMITMENTS</p><h2>Every source event counted once</h2></div><Badge variant="outline" className={commitments.disjoint ? '' : 'review-badge'}>{commitments.disjoint ? `${commitments.distinct_source_ids} unique sources` : 'Duplicate source'}</Badge></div>
    <div className="commitment-grid">{Object.entries(commitments.groups).map(([key, group]) => <section key={key}><span>{groupLabels[key as keyof typeof groupLabels]}</span><strong>{money(group.amount, page.currency)}</strong><small>{group.cash_effect === 'internal_earmark' ? 'Internal earmark · not a cash outflow' : 'External cash outflow'}</small><details><summary>{group.items.length} source event{group.items.length === 1 ? '' : 's'}</summary><ul>{group.items.slice(0, 12).map(item => <li key={item.source_id}><span>{item.label}{item.tier ? ` · Tier ${item.tier}` : ''}</span><b>{money(item.amount, page.currency)}</b></li>)}</ul></details></section>)}</div>
    <div className="controls-equation"><span>Free cash after commitments</span><strong>{money(page.free_cash_after_commitments?.amount, page.currency)}</strong><small>{page.free_cash_after_commitments?.formula}. This is not a spending allowance and does not assume future income.</small></div>
  </Card>
}

function CapacityPanel({ page }: { page: SpendingControlsPage }) {
  const capacity = page.capacity!
  return <Card className="controls-card"><div className="panel-title"><div><p className="eyebrow">CONSERVATIVE CAPACITY</p><h2>{capacity.status === 'capacity' ? `Additional-spending capacity ${money(capacity.additional_spending_capacity, page.currency)}` : capacity.status === 'funding_need' ? `Funding need ${money(capacity.funding_need, page.currency)}` : 'No definitive allowance'}</h2></div><Badge variant="outline" className={capacity.status === 'capacity' ? '' : 'review-badge'}>{capacity.status.replaceAll('_', ' ')}</Badge></div>
    <p className="muted-note">Minimum conservative unallocated, reserve-adjusted balance: {money(capacity.minimum_conservative_unallocated_reserve_adjusted_balance, page.currency)}.</p>
    {capacity.status === 'unresolved' && <ul className="issue-list">{capacity.reasons.map(reason => <li key={reason}>{reason}</li>)}</ul>}
    {capacity.weekly && <details className="weekly-schedule"><summary><CalendarClock size={15} /> Weekly path: {money(capacity.weekly.amount_per_week, page.currency)} · entire path verified</summary><ul>{capacity.weekly.schedule.map(row => <li key={row.on}><span>{row.on}</span><b>{money(row.amount, page.currency)}</b></li>)}</ul></details>}
  </Card>
}

function BreachDrivers({ page }: { page: SpendingControlsPage }) {
  const breach = page.first_breach_drivers!
  return <Card className="controls-card breach-drivers"><div className="panel-title"><div><p className="eyebrow">FIRST BREACH · {breach.on}</p><h2>What drives the low point</h2></div></div>
    <div className="driver-columns"><section><h3>Events that day</h3>{breach.events.length ? <ul>{breach.events.map(event => <li key={event.event_id}><span>{event.label} · {money(event.amount, page.currency)}</span><a href={event.edit_href}>Review source</a></li>)}</ul> : <p>No single dated event; distributed lifestyle spending reaches the threshold.</p>}</section><section><h3>Later income assumptions</h3>{breach.later_income_assumptions.length ? <ul>{breach.later_income_assumptions.map(event => <li key={event.event_id}><span>{event.label} · {money(event.amount, page.currency)} on {event.on}</span><a href={event.edit_href}>Review assumption</a></li>)}</ul> : <p>No later included income assumption.</p>}</section></div>
  </Card>
}

function WhatIfForm({ page, onResult, onError }: { page: SpendingControlsPage; onResult: (page: SpendingControlsPage) => void; onError: (message: string) => void }) {
  const [mode, setMode] = useState<Mode>('cut')
  const [category, setCategory] = useState('')
  const [eventId, setEventId] = useState('')
  const [amount, setAmount] = useState('')
  const [on, setOn] = useState('')
  const [label, setLabel] = useState('Hypothetical income')
  const [busy, setBusy] = useState(false)
  const categories = page.what_if_options.categories
  const events = page.what_if_options.events

  async function submit(event: FormEvent) {
    event.preventDefault(); onError(''); setBusy(true)
    const draft: WhatIfDraft = { month: page.month, currency: page.currency, category_cuts: [], date_changes: [], hypothetical_income: [] }
    if (mode === 'cut') draft.category_cuts.push({ category_id: category, monthly_reduction: amount })
    if (mode === 'date') draft.date_changes.push({ event_id: eventId, new_date: on })
    if (mode === 'income') draft.hypothetical_income.push({ amount, currency: page.currency, on, label })
    try { onResult(await runSpendingWhatIf(draft)) } catch (reason) { onError((reason as Error).message) } finally { setBusy(false) }
  }

  return <Card className="controls-card what-if-card"><div className="panel-title"><div><p className="eyebrow">WHAT IF</p><h2>Test one proposed change</h2></div><SlidersHorizontal size={19} /></div>
    <form onSubmit={submit}><label>Change to model<select value={mode} onChange={event => { setMode(event.target.value as Mode); setAmount(''); setOn('') }}><option value="cut">Reduce a Tier 3/4 expense</option><option value="date">Change an event date</option><option value="income">Add hypothetical income</option></select></label>
      {mode === 'cut' && <><label>Source-backed category<select required value={category} onChange={event => { setCategory(event.target.value); setAmount('') }}><option value="">Choose Tier 3/4 category</option>{categories.map(option => <option key={option.category_id} value={option.category_id}>Tier {option.tier} · {shortCategory(option.category_key)} · {money(option.source_backed_monthly, page.currency)}/month observed</option>)}</select></label><label>Proposed monthly reduction<Input required inputMode="decimal" pattern="[0-9]+(\.[0-9]{1,2})?" value={amount} onChange={event => setAmount(event.target.value)} /><small>The model caps a cut at spending that remains in each month. Tier 1/2 spending is unchanged.</small></label>{category && <Button type="button" variant="outline" size="sm" onClick={() => setAmount(categories.find(option => option.category_id === category)?.source_backed_monthly ?? '')}>Use full observed amount</Button>}</>}
      {mode === 'date' && <><label>Source-backed event<select required value={eventId} onChange={event => setEventId(event.target.value)}><option value="">Choose event</option>{events.map(option => <option key={option.event_id} value={option.event_id}>{option.protected_tier_1_or_2 ? 'Protected Tier 1/2 · ' : ''}{option.label} · {option.on}</option>)}</select><small>Selecting a protected Tier 1/2 event is the explicit change required to move it.</small></label><label>Proposed date<Input required type="date" value={on} onChange={event => setOn(event.target.value)} /></label></>}
      {mode === 'income' && <><label>Label<Input required maxLength={120} value={label} onChange={event => setLabel(event.target.value)} /></label><div className="form-row"><label>Hypothetical amount<Input required inputMode="decimal" pattern="[0-9]+(\.[0-9]{1,2})?" value={amount} onChange={event => setAmount(event.target.value)} /></label><label>Date<Input required type="date" value={on} onChange={event => setOn(event.target.value)} /></label></div></>}
      <p className="muted-note">This recomputes base and conservative cases in isolation. Nothing is saved, classified as Confirmed, or described as completed.</p><div className="dialog-actions"><Button type="submit" disabled={busy || (mode === 'cut' && categories.length === 0) || (mode === 'date' && events.length === 0)}>{busy ? 'Recomputing…' : 'Compare before and after'}</Button></div>
    </form>
    {page.scenario && <div className="scenario-result" role="status"><div className="alert"><AlertTriangle size={17} /><div><strong>Hypothetical result only</strong><p>{page.scenario.statement}</p></div></div><div className="comparison-grid">{(['base', 'conservative'] as const).map(name => { const comparison = page.scenario!.comparison[name]; return <section key={name}><span>{name} case</span><strong>{money(comparison.before_gap, page.currency)} → {money(comparison.after_gap, page.currency)} gap</strong><small>Cash-path change {money(comparison.cash_improvement, page.currency)} · unchanged case retained for comparison</small></section> })}</div>{page.scenario.unresolved.length > 0 && <><h3>Still unresolved</h3><ul className="issue-list">{page.scenario.unresolved.map(item => <li key={item}>{item}</li>)}</ul></>}</div>}
  </Card>
}

export function ControlsSummary({ page }: { page: SpendingControlsPage }) {
  return <span>{page.capacity?.status === 'capacity' ? <><CheckCircle2 size={14} /> {money(page.capacity.additional_spending_capacity, page.currency)}</> : <><AlertTriangle size={14} /> Controls unresolved</>}</span>
}
