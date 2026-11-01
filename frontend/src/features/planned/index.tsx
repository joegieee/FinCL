import { useCallback, useEffect, useMemo, useState } from 'react'
import { CalendarClock, Plus, X } from 'lucide-react'
import { AsyncView } from '@/components/AsyncView'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { money } from '@/lib/api'
import { useBatchCommand, useLedgerCommand } from '@/lib/command'
import { dateLabel } from '@/lib/format'
import { reportedMoney, WorkspaceReportingDetails } from '@/lib/workspaceReporting'
import {
  flowSet, loadAccountOptions, loadCandidates, loadImpact, loadPlanned, loadTripOptions, matchRetire, matchSet, monthLabel,
  monthlyRows, monthWindow, nextKey, resolveRetire, resolveSet, retireFlowItems, validAmount,
  type AccountOption, type Certainty, type Direction, type FlowDraft, type MatchCandidates, type OccurrenceDraft,
  type OccurrenceState, type PlannedFlow, type PlannedImpact, type PlannedOccurrence, type PlannedPage, type Treatment, type TripOption,
} from './api'

type Saved = (status: string) => void | Promise<void>

const STATE_LABEL: Record<OccurrenceState, string> = {
  upcoming: 'Upcoming', partly_matched: 'Partly matched', matched: 'Matched', overdue: 'Overdue', done: 'Done', cancelled: 'Didn’t happen',
}
const CERTAINTY_LABEL: Record<Certainty, string> = { sure: 'Sure', expected: 'Expected', maybe: 'Maybe' }
const CERTAINTY_HELP: Record<Direction, Record<Certainty, string>> = {
  outflow: {
    sure: 'Counted in both the base and the cautious answer.',
    expected: 'Counted in both the base and the cautious answer.',
    maybe: 'Counted only in the cautious answer.',
  },
  inflow: {
    sure: 'Counted in both the base and the cautious answer.',
    expected: 'Counted in the base answer only; the cautious answer does not rely on it.',
    maybe: 'Shown as possible upside and never counted.',
  },
}
const TREATMENT_LABEL: Record<Treatment, string> = {
  extra: 'On top of my usual spending',
  irregular_allowance: 'Comes out of my allowance for one-off costs',
}

function when(occurrence: { earliest: string; latest: string }): string {
  if (occurrence.earliest === occurrence.latest) return dateLabel(occurrence.earliest)
  const window = monthWindow(occurrence.earliest.slice(0, 7))
  if (window.earliest === occurrence.earliest && window.latest === occurrence.latest) return `Sometime in ${monthLabel(occurrence.earliest.slice(0, 7))}`
  return `${dateLabel(occurrence.earliest)} – ${dateLabel(occurrence.latest)}`
}

/**
 * Money you know is coming or going that history cannot show: a purchase next
 * month, a course paid in instalments, an expected bonus. Each date is checked
 * against what was actually paid once it passes.
 */
export function PlannedView({ generation, currencies, categories, onSaved }: {
  generation: string; currencies: string[]; categories: string[]; onSaved: Saved
}) {
  const [page, setPage] = useState<PlannedPage | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [editing, setEditing] = useState<PlannedFlow | 'new' | null>(null)
  const [accounts, setAccounts] = useState<AccountOption[]>([])
  const [trips, setTrips] = useState<TripOption[]>([])

  const load = useCallback(() => {
    setLoading(true); setError('')
    loadPlanned().then(setPage).catch(e => setError((e as Error).message)).finally(() => setLoading(false))
  }, [])
  useEffect(() => { load() }, [load, generation])
  useEffect(() => {
    loadAccountOptions().then(setAccounts).catch(() => {})
    loadTripOptions().then(setTrips).catch(() => {})
  }, [])

  async function saved(status: string) { setEditing(null); await onSaved(status) }

  return <div className="planned-view">
    <AsyncView loading={loading} error={error} data={page} onRetry={load} errorTitle="We couldn’t load your planned items.">
      {data => {
        const flows = new Map(data.flows.map(flow => [flow.flow, flow]))
        const find = (flow: string, key: string) => {
          const owner = flows.get(flow)
          const occurrence = owner?.occurrences.find(item => item.key === key)
          return owner && occurrence ? { owner, occurrence } : null
        }
        return <>
          <WorkspaceReportingDetails data={data}/>
          <div className="row-actions"><Button onClick={() => setEditing('new')}><Plus size={15}/> Add planned spending or income</Button></div>
          {editing && <FlowForm key={editing === 'new' ? 'new' : editing.flow} initial={editing === 'new' ? null : editing} currencies={currencies} categories={categories}
            accounts={accounts} trips={trips} generation={generation} onSaved={saved} onCancel={() => setEditing(null)}/>}
          {!data.cutoff && <p className="muted-note">There is no supported cash cutoff yet, so nothing here reaches the forecast. Import statements to start one.</p>}
          {data.attention.length > 0 && <Card className="controls-card planned-attention">
            <div className="panel-title"><div><p className="eyebrow">Needs an answer</p><h2>These dates have passed</h2>
              <p className="muted-note">Each is judged against your statements up to {data.cutoff ? dateLabel(data.cutoff) : 'the latest import'}. Spending stays in the forecast until you say what happened; income is left out until it arrives.</p></div></div>
            {data.attention.map(item => {
              const found = find(item.flow, item.occurrence)
              return found && <OccurrenceRow key={`${item.flow}:${item.occurrence}`} flow={found.owner} occurrence={found.occurrence} question={item.question}
                generation={generation} onSaved={onSaved} onEdit={() => setEditing(found.owner)}/>
            })}
          </Card>}
          {data.flows.length === 0 && !editing && <Card className="controls-card"><p className="muted-note">Nothing planned yet. Add a purchase, a bill, a course paid in instalments or money you expect to receive, and the forecast will count it.</p></Card>}
          {data.months.map(month => <Card key={month.month} className="controls-card planned-month">
            <div className="panel-title"><div><p className="eyebrow">{month.month}</p><h2>{monthLabel(month.month)}</h2></div>
              <div className="planned-totals">{Object.entries(month.totals).map(([currency, totals]) => <small key={currency}>
                Still to come · out {reportedMoney(totals, 'outflows', currency)} · in {reportedMoney(totals, 'inflows', currency)}</small>)}</div></div>
            {month.items.map(item => {
              const found = find(item.flow, item.occurrence)
              return found && <OccurrenceRow key={`${item.flow}:${item.occurrence}`} flow={found.owner} occurrence={found.occurrence}
                generation={generation} onSaved={onSaved} onEdit={() => setEditing(found.owner)}/>
            })}
          </Card>)}
          {data.rules.length > 0 && <ul className="muted-note planned-rules">{data.rules.map(rule => <li key={rule}>{rule}</li>)}</ul>}
        </>
      }}
    </AsyncView>
  </div>
}

type Mode = 'day' | 'month' | 'monthly' | 'rows' | 'trip'

/** A new trip budget starts from the trip: linked to it and dated over its days. */
export type TripBudgetPrefill = { tripId: string; name: string; start: string; end: string; currency?: string }

function FlowForm({ initial, prefill, currencies, categories, accounts, trips, generation, onSaved, onCancel }: {
  initial: PlannedFlow | null; prefill?: TripBudgetPrefill; currencies: string[]; categories: string[]; accounts: AccountOption[]; trips: TripOption[]
  generation: string; onSaved: Saved; onCancel: () => void
}) {
  const [flowId] = useState(() => initial?.flow ?? crypto.randomUUID())
  const [direction, setDirection] = useState<Direction>(initial?.direction ?? 'outflow')
  const [name, setName] = useState(initial?.name ?? (prefill ? `${prefill.name} budget` : ''))
  const [currency, setCurrency] = useState(initial?.currency ?? prefill?.currency ?? currencies[0] ?? 'SGD')
  const [mode, setMode] = useState<Mode>(initial || prefill ? 'rows' : 'day')
  const [amount, setAmount] = useState('')
  const [day, setDay] = useState('')
  const [month, setMonth] = useState('')
  const [count, setCount] = useState('3')
  const [rows, setRows] = useState<OccurrenceDraft[]>(() => initial?.occurrences.map(o => ({ key: o.key, earliest: o.earliest, latest: o.latest, amount: o.planned }))
    ?? (prefill ? [{ key: '1', earliest: prefill.start, latest: prefill.end, amount: '' }] : []))
  const [account, setAccount] = useState(initial?.account_id ?? '')
  const [category, setCategory] = useState(initial?.category ?? '')
  const [certainty, setCertainty] = useState<Certainty>(initial?.certainty ?? 'sure')
  const [treatment, setTreatment] = useState<Treatment>(initial?.baseline_treatment ?? 'extra')
  const [trip, setTrip] = useState(initial?.link?.id ?? prefill?.tripId ?? '')
  const [reason, setReason] = useState('')
  const [impact, setImpact] = useState<PlannedImpact | null>(null)
  const [impactError, setImpactError] = useState('')
  const cmd = useLedgerCommand(generation, onSaved)
  const outflow = direction === 'outflow'
  const linked = outflow ? trips.find(t => t.trip_id === trip) : undefined

  const occurrences: OccurrenceDraft[] = mode === 'day' ? (day ? [{ key: '1', earliest: day, latest: day, amount }] : [])
    : mode === 'month' ? (month ? [{ key: '1', ...monthWindow(month), amount }] : [])
    : mode === 'trip' ? (linked ? [{ key: '1', earliest: linked.start_on, latest: linked.end_on, amount }] : [])
    : rows
  const rowsValid = occurrences.length > 0 && occurrences.every(row => row.earliest && row.latest && row.earliest <= row.latest && validAmount(row.amount))
  const draft: FlowDraft = {
    name: name.trim(), direction, currency, account_id: account || null, category: category || null,
    occurrences, certainty, baseline_treatment: outflow ? treatment : 'extra',
    link: outflow && trip ? { kind: 'trip', id: trip } : null, reason: reason.trim(),
  }
  const ready = Boolean(draft.name && draft.reason && rowsValid)
  const changed = () => cmd.reset()
  const draftKey = JSON.stringify(draft)
  useEffect(() => {
    setImpact(null); setImpactError('')
    if (!cmd.previewed) return
    let current = true
    loadImpact(flowId, draft).then(result => { if (current) setImpact(result) }).catch(e => { if (current) setImpactError((e as Error).message) })
    return () => { current = false }
  }, [cmd.previewed, draftKey, flowId])
  function pickTrip(id: string) {
    setTrip(id); changed()
    const picked = trips.find(t => t.trip_id === id)
    if (picked && !initial && (mode === 'trip' || (mode === 'day' && !day) || (mode === 'month' && !month))) setMode('trip')
    else if (!picked && mode === 'trip') setMode('day')
  }
  function edit<T>(set: (value: T) => void) { return (value: T) => { set(value); changed() } }
  function editRow(index: number, patch: Partial<OccurrenceDraft>) {
    setRows(current => current.map((row, i) => i === index ? { ...row, ...patch, ...(patch.earliest && row.earliest === row.latest ? { latest: patch.earliest } : {}) } : row)); changed()
  }

  return <Card className="controls-card trip-form planned-form">
    <div className="panel-title"><div><p className="eyebrow">{initial ? 'Edit planned item' : 'New planned item'}</p><h2>{initial ? initial.name : 'What do you know is coming?'}</h2></div>
      <Button variant="ghost" size="sm" onClick={onCancel} aria-label="Close planned item form"><X size={15}/></Button></div>
    <div className="form-grid">
      <fieldset className="planned-choice" disabled={Boolean(initial)}><legend>Money</legend>
        <label><input type="radio" name="direction" checked={outflow} onChange={() => edit(setDirection)('outflow')}/> Going out</label>
        <label><input type="radio" name="direction" checked={!outflow} onChange={() => edit(setDirection)('inflow')}/> Coming in</label>
      </fieldset>
      <label>Name<input value={name} maxLength={80} onChange={e => edit(setName)(e.target.value)} placeholder={outflow ? 'New laptop' : 'Year-end bonus'}/></label>
      <label>Currency<select value={currency} disabled={Boolean(initial)} onChange={e => edit(setCurrency)(e.target.value)}>
        {[...new Set([currency, ...currencies])].map(code => <option key={code} value={code}>{code}</option>)}</select></label>
      <label>When<select value={mode} onChange={e => edit(setMode)(e.target.value as Mode)}>
        <option value="day">On a date</option><option value="month">Sometime in a month</option>
        <option value="monthly">Every month for a while</option>{linked && <option value="trip">During the trip</option>}{(initial || mode === 'rows') && <option value="rows">Dates listed below</option>}</select></label>
      {mode === 'day' && <label>Date<input type="date" value={day} onChange={e => edit(setDay)(e.target.value)}/></label>}
      {mode === 'month' && <label>Month<input type="month" value={month} onChange={e => edit(setMonth)(e.target.value)}/></label>}
      {mode === 'monthly' && <label>First date<input type="date" value={day} onChange={e => edit(setDay)(e.target.value)}/></label>}
      {mode === 'monthly' && <label>How many months<input type="number" min={1} max={60} value={count} onChange={e => edit(setCount)(e.target.value)}/></label>}
      {mode === 'trip' && linked && <p className="muted-note">{dateLabel(linked.start_on)} – {dateLabel(linked.end_on)}, the dates of {linked.name}.</p>}
      {mode !== 'rows' && <label>Amount<input inputMode="decimal" value={amount} onChange={e => edit(setAmount)(e.target.value.trim())} placeholder="1200.00"/></label>}
      <label>{outflow ? 'Paid from' : 'Paid into'}<select value={account} onChange={e => edit(setAccount)(e.target.value)}>
        <option value="">Not decided</option>
        {accounts.filter(a => a.currencies.includes(currency) || a.id === account).map(a => <option key={a.id} value={a.id}>{a.display_name}</option>)}</select></label>
      <label>Category<select value={category} onChange={e => edit(setCategory)(e.target.value)}>
        <option value="">None</option>{categories.filter(c => !c.startsWith('Internal')).map(c => <option key={c} value={c}>{c}</option>)}</select></label>
      <label>How sure<select value={certainty} onChange={e => edit(setCertainty)(e.target.value as Certainty)} aria-describedby="planned-certainty-help">
        {(Object.keys(CERTAINTY_LABEL) as Certainty[]).map(c => <option key={c} value={c}>{CERTAINTY_LABEL[c]}</option>)}</select></label>
      {outflow && <label>Compared with usual spending<select value={treatment} onChange={e => edit(setTreatment)(e.target.value as Treatment)}>
        {(Object.keys(TREATMENT_LABEL) as Treatment[]).map(t => <option key={t} value={t}>{TREATMENT_LABEL[t]}</option>)}</select></label>}
      {outflow && <label>Trip<select value={trip} onChange={e => pickTrip(e.target.value)}>
        <option value="">Not part of a trip</option>{trips.map(t => <option key={t.trip_id} value={t.trip_id}>{t.name}</option>)}</select></label>}
      <label>Reason<input value={reason} onChange={e => edit(setReason)(e.target.value)} placeholder="Why you expect it"/></label>
    </div>
    <p id="planned-certainty-help" className="muted-note">{CERTAINTY_HELP[direction][certainty]}{outflow && treatment === 'irregular_allowance' && ' It replaces that month’s allowance for one-off costs instead of adding to it.'}{outflow && trip && ' A trip’s spending settles it automatically, so it is never matched by hand.'}</p>
    {mode === 'monthly' && <div className="row-actions"><Button variant="outline" size="sm" disabled={!day || !validAmount(amount) || !/^([1-9]|[1-5]\d|60)$/.test(count)}
      onClick={() => { setRows(monthlyRows(day, parseInt(count, 10), amount)); setMode('rows'); changed() }}>List the months</Button></div>}
    {mode === 'rows' && <div className="table-scroll"><table className="pocket-table planned-rows"><thead><tr><th>From</th><th>To</th><th>Amount ({currency})</th><th><span className="sr-only">Remove</span></th></tr></thead>
      <tbody>{rows.map((row, index) => <tr key={row.key}>
        <td><input type="date" aria-label={`Date ${index + 1}`} value={row.earliest} onChange={e => editRow(index, { earliest: e.target.value })}/></td>
        <td><input type="date" aria-label={`Until ${index + 1}`} value={row.latest} onChange={e => editRow(index, { latest: e.target.value })}/></td>
        <td><input inputMode="decimal" aria-label={`Amount ${index + 1}`} value={row.amount} onChange={e => editRow(index, { amount: e.target.value.trim() })}/></td>
        <td><Button variant="ghost" size="sm" aria-label={`Remove date ${index + 1}`} disabled={rows.length === 1} onClick={() => { setRows(current => current.filter((_, i) => i !== index)); changed() }}><X size={13}/></Button></td>
      </tr>)}</tbody></table>
      <Button variant="ghost" size="sm" onClick={() => { const last = rows.at(-1); setRows(current => [...current, { key: nextKey(current), earliest: last?.earliest ?? '', latest: last?.latest ?? '', amount: last?.amount ?? '' }]); changed() }}><Plus size={13}/> Add a date</Button></div>}
    {occurrences.some(row => row.amount && !validAmount(row.amount)) && <p className="form-error" role="alert">Amounts are positive, with at most two decimal places.</p>}
    {cmd.previewed && <div className="preview" role="status"><p>{initial ? 'Updates' : 'Adds'} “{draft.name}”, {outflow ? 'going out' : 'coming in'}: {draft.occurrences.map(row => `${money(row.amount, currency)} (${when(row)})`).join(', ')}.
      {' '}{outflow ? `${TREATMENT_LABEL[draft.baseline_treatment]}. ` : ''}{CERTAINTY_LABEL[certainty]}: {CERTAINTY_HELP[direction][certainty]}</p></div>}
    {cmd.previewed && <ImpactSummary impact={impact} error={impactError}/>}
    {cmd.error && <p className="form-error" role="alert">{cmd.error}</p>}
    <div className="row-actions"><Button disabled={!ready || cmd.busy} onClick={() => void cmd.act(flowSet(flowId, draft))}>{cmd.busy ? 'Working…' : cmd.previewed ? 'Save planned item' : 'Preview planned item'}</Button></div>
  </Card>
}

function ImpactSummary({ impact, error }: { impact: PlannedImpact | null; error: string }) {
  if (error) return <p className="muted-note" role="status">We couldn’t work out the effect on your answer: {error}</p>
  if (!impact) return <p className="muted-note" role="status">Working out the effect on your answer…</p>
  const { before, after } = impact
  const cases = [['Base case', 'base'], ['Cautious case', 'cautious']] as const
  const lowest = (answer: PlannedImpact['after'], key: 'base' | 'cautious') => {
    const cell = answer[key]
    return cell.lowest ? `${money(cell.lowest, answer.reporting_currency ?? impact.currency)}${cell.lowest_on ? ` on ${dateLabel(cell.lowest_on)}` : ''}` : 'not available'
  }
  return <div className="preview planned-impact" role="status">
    <p><strong>Will I have enough?</strong> {before.answer ?? 'Not available'} before, <strong>{after.answer ?? 'not available'}</strong> with this{impact.flips ? ' — this changes the answer.' : '.'}</p>
    <ul>{cases.map(([label, key]) => <li key={key}>{label}: lowest point {lowest(before, key)} before, {lowest(after, key)} with this.</li>)}</ul>
    {impact.months.length > 0 && <div className="table-scroll"><table className="pocket-table"><thead><tr><th>Month</th><th>Base case closing balance changes by</th><th>Cautious case closing balance changes by</th></tr></thead>
      <tbody>{impact.months.map(row => <tr key={row.month}><td>{monthLabel(row.month)}</td><td>{row.base.change ? money(row.base.change, impact.currency) : '—'}</td><td>{row.cautious.change ? money(row.cautious.change, impact.currency) : '—'}</td></tr>)}</tbody></table></div>}
  </div>
}

/**
 * The planned-item form on its own, for a page that offers it in place (a
 * trip's budget): it fetches the choices the form needs. With `flow` it edits
 * that item; otherwise it starts a new one from `prefill`.
 */
export function PlannedFlowEditor({ flow, prefill, currencies, categories, generation, onSaved, onCancel }: {
  flow?: string; prefill?: TripBudgetPrefill; currencies: string[]; categories: string[]
  generation: string; onSaved: Saved; onCancel: () => void
}) {
  const [accounts, setAccounts] = useState<AccountOption[]>([])
  const [trips, setTrips] = useState<TripOption[]>([])
  const [initial, setInitial] = useState<PlannedFlow | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    loadAccountOptions().then(setAccounts).catch(() => {})
    loadTripOptions().then(setTrips).catch(() => {})
  }, [])
  useEffect(() => {
    if (!flow) return
    loadPlanned().then(page => {
      const found = page.flows.find(item => item.flow === flow)
      if (found) setInitial(found); else setError('That planned item no longer exists.')
    }).catch(e => setError((e as Error).message))
  }, [flow])
  if (error) return <p className="form-error" role="alert">{error}</p>
  if (flow && !initial) return <p className="muted-note">Loading the budget…</p>
  return <FlowForm initial={initial} prefill={prefill} currencies={currencies} categories={categories}
    accounts={accounts} trips={trips} generation={generation} onSaved={onSaved} onCancel={onCancel}/>
}

type Action = 'match' | 'done' | 'cancelled' | 'reopen' | 'remove' | null

function OccurrenceRow({ flow, occurrence, question, generation, onSaved, onEdit }: {
  flow: PlannedFlow; occurrence: PlannedOccurrence; question?: string; generation: string; onSaved: Saved; onEdit: () => void
}) {
  const [action, setAction] = useState<Action>(null)
  const open = !['done', 'cancelled', 'matched'].includes(occurrence.state)
  const matchable = !flow.link && occurrence.state !== 'done' && occurrence.state !== 'cancelled'
  const toggle = (next: Action) => setAction(current => current === next ? null : next)
  return <div className="budget-row planned-row">
    <div className="budget-head">
      <div>
        <strong><CalendarClock size={13}/> {flow.name}</strong> <Badge variant="outline" className={occurrence.state === 'overdue' ? 'review-badge' : ''}>{STATE_LABEL[occurrence.state]}</Badge>
        <small>{when(occurrence)} · {flow.direction === 'outflow' ? 'out' : 'in'} {reportedMoney(occurrence, 'planned', flow.currency)}
          {occurrence.matched !== '0.00' && <> · {flow.link ? 'spent on the trip' : 'matched'} {reportedMoney(occurrence, 'matched', flow.currency)}</>}
          {open && occurrence.matched !== '0.00' && <> · still to come {reportedMoney(occurrence, 'remaining', flow.currency)}</>}
          {flow.link && <> · trip {flow.link.name ?? 'no longer recorded'}</>}</small>
        {occurrence.forecast_note && <small>{occurrence.forecast_note}</small>}
        {occurrence.outcome && <small>{occurrence.outcome.reason}</small>}
        {question && <small><strong>{question}</strong></small>}
      </div>
      <div className="row-actions planned-actions">
        {matchable && open && <Button variant="outline" size="sm" onClick={() => toggle('match')}>Match a payment</Button>}
        {open && <Button variant="outline" size="sm" onClick={onEdit}>Move date</Button>}
        {open && occurrence.matched === '0.00' && <Button variant="outline" size="sm" onClick={() => toggle('cancelled')}>Didn’t happen</Button>}
        {open && <Button variant="outline" size="sm" onClick={() => toggle('done')}>Done</Button>}
        {!open && occurrence.outcome && <Button variant="ghost" size="sm" onClick={() => toggle('reopen')}>Reopen</Button>}
        {!open && !occurrence.outcome && matchable && <Button variant="ghost" size="sm" onClick={() => toggle('match')}>Matches</Button>}
        <Button variant="ghost" size="sm" onClick={() => toggle('remove')}>Remove item</Button>
      </div>
    </div>
    {action === 'match' && <MatchPicker flow={flow} occurrence={occurrence} generation={generation} onSaved={async s => { setAction(null); await onSaved(s) }}/>}
    {(action === 'done' || action === 'cancelled' || action === 'reopen') && <OutcomeForm action={action} flow={flow} occurrence={occurrence} generation={generation} onSaved={async s => { setAction(null); await onSaved(s) }}/>}
    {action === 'remove' && <RemoveFlow flow={flow} generation={generation} onSaved={async s => { setAction(null); await onSaved(s) }}/>}
  </div>
}

const OUTCOME_COPY = {
  done: { button: 'Mark done', preview: 'Marks this date done: whatever is not matched is not expected any more.' },
  cancelled: { button: 'Mark as didn’t happen', preview: 'Marks this date as not happening; it leaves the forecast.' },
  reopen: { button: 'Reopen', preview: 'Reopens this date; it is judged against your statements again.' },
}

function OutcomeForm({ action, flow, occurrence, generation, onSaved }: {
  action: 'done' | 'cancelled' | 'reopen'; flow: PlannedFlow; occurrence: PlannedOccurrence; generation: string; onSaved: Saved
}) {
  const [reason, setReason] = useState('')
  const cmd = useLedgerCommand(generation, onSaved)
  const draft = action === 'reopen' ? resolveRetire(flow.flow, occurrence.key, reason.trim()) : resolveSet(flow.flow, occurrence.key, action, reason.trim())
  return <div className="planning-form">
    <label>Reason<input value={reason} onChange={e => { setReason(e.target.value); cmd.reset() }} placeholder="What happened"/></label>
    {cmd.previewed && <div className="preview" role="status"><p>{OUTCOME_COPY[action].preview}</p></div>}
    {cmd.error && <p className="form-error" role="alert">{cmd.error}</p>}
    <Button size="sm" disabled={!reason.trim() || cmd.busy} onClick={() => void cmd.act(draft)}>{cmd.busy ? 'Working…' : cmd.previewed ? `Save: ${OUTCOME_COPY[action].button.toLowerCase()}` : OUTCOME_COPY[action].button}</Button>
  </div>
}

function MatchPicker({ flow, occurrence, generation, onSaved }: { flow: PlannedFlow; occurrence: PlannedOccurrence; generation: string; onSaved: Saved }) {
  const [data, setData] = useState<MatchCandidates | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [entry, setEntry] = useState('')
  const [amount, setAmount] = useState('')
  const [reason, setReason] = useState('')
  const cmd = useLedgerCommand(generation, onSaved)
  const unmatch = useBatchCommand(generation, onSaved)
  const load = useCallback(() => {
    setLoading(true); setError('')
    loadCandidates(flow.flow, occurrence.key).then(setData).catch(e => setError((e as Error).message)).finally(() => setLoading(false))
  }, [flow.flow, occurrence.key])
  useEffect(() => { load() }, [load])
  const chosen = useMemo(() => data?.candidates.find(c => c.entry === entry) ?? null, [data, entry])

  return <div className="planning-form planned-match">
    {occurrence.matches.length > 0 && <div className="table-scroll"><table className="pocket-table"><thead><tr><th>Matched payment</th><th>Amount</th><th><span className="sr-only">Unmatch</span></th></tr></thead>
      <tbody>{occurrence.matches.map(match => <tr key={match.entry}>
        <td>{match.dangling ? 'A transaction that no longer resolves' : `${match.description} · ${match.on ? dateLabel(match.on) : ''}`}<small>{match.account}</small></td>
        <td>{reportedMoney(match, 'amount', flow.currency)}</td>
        <td><Button variant="ghost" size="sm" disabled={unmatch.busy} onClick={() => void unmatch.submit([matchRetire(flow.flow, occurrence.key, match.entry, `Unmatched from ${flow.name}`)], `Unmatched from ${flow.name}`)}>Unmatch</Button></td>
      </tr>)}</tbody></table></div>}
    {unmatch.error && <p className="form-error" role="alert">{unmatch.error}</p>}
    <AsyncView loading={loading} error={error} data={data} onRetry={load} errorTitle="We couldn’t load matching payments.">
      {page => page.candidates.length === 0
        ? <p className="muted-note">No {flow.currency} {flow.direction === 'outflow' ? 'payments' : 'receipts'} between {dateLabel(page.window.from)} and {dateLabel(page.window.to)} are left to match.</p>
        : <fieldset className="planned-candidates"><legend>Payments near {when(occurrence).toLowerCase()} · {money(page.remaining, flow.currency)} left to match</legend>
          {page.candidates.map(candidate => <label key={candidate.entry} className="planned-candidate">
            <input type="radio" name={`match-${flow.flow}-${occurrence.key}`} checked={entry === candidate.entry}
              onChange={() => { setEntry(candidate.entry); setAmount(candidate.suggested_amount); cmd.reset() }}/>
            <span>{dateLabel(candidate.on)} · {candidate.merchant} · {money(candidate.amount, flow.currency)}<small>{candidate.account}{candidate.available !== candidate.amount && ` · ${money(candidate.available, flow.currency)} not yet matched elsewhere`}{candidate.matched_here && ' · already matched here'}</small></span>
          </label>)}
        </fieldset>}
    </AsyncView>
    {chosen && <div className="form-grid">
      <label>Amount to count<input inputMode="decimal" value={amount} onChange={e => { setAmount(e.target.value.trim()); cmd.reset() }}/></label>
      <label>Reason<input value={reason} onChange={e => { setReason(e.target.value); cmd.reset() }} placeholder="Why this payment settles it"/></label>
    </div>}
    {chosen && amount && !validAmount(amount) && <p className="form-error" role="alert">Amounts are positive, with at most two decimal places.</p>}
    {cmd.previewed && chosen && <div className="preview" role="status"><p>Counts {money(amount, flow.currency)} of {chosen.merchant} on {dateLabel(chosen.on)} towards {flow.name}.</p></div>}
    {cmd.error && <p className="form-error" role="alert">{cmd.error}</p>}
    {chosen && <Button size="sm" disabled={!validAmount(amount) || !reason.trim() || cmd.busy} onClick={() => void cmd.act(matchSet(flow.flow, occurrence.key, chosen.entry, amount, reason.trim()))}>
      {cmd.busy ? 'Working…' : cmd.previewed ? 'Save match' : 'Preview match'}</Button>}
  </div>
}

function RemoveFlow({ flow, generation, onSaved }: { flow: PlannedFlow; generation: string; onSaved: Saved }) {
  const [reason, setReason] = useState('')
  const batch = useBatchCommand(generation, onSaved)
  const matches = flow.link ? 0 : flow.occurrences.reduce((count, o) => count + o.matches.length, 0)
  return <div className="planning-form">
    <p className="muted-note">Removes “{flow.name}” and all {flow.occurrences.length} of its dates from the forecast.{matches > 0 && ` Its ${matches} matched payment(s) are unmatched in the same save; the transactions themselves are kept.`}</p>
    <label>Reason<input value={reason} onChange={e => { setReason(e.target.value); batch.invalidate() }} placeholder="Why it is no longer planned"/></label>
    {batch.error && <div className="alert error" role="alert"><p>{batch.error}</p>{batch.submitted && <Button variant="outline" onClick={() => void batch.retry()}>Retry same save</Button>}</div>}
    <Button size="sm" variant="outline" disabled={!reason.trim() || batch.busy} onClick={() => void batch.submit(retireFlowItems(flow, reason.trim()), reason.trim())}>{batch.busy ? 'Removing…' : 'Remove planned item'}</Button>
  </div>
}
