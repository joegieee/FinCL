import { useCallback, useEffect, useMemo, useState } from 'react'
import { MapPinned, Plus, X } from 'lucide-react'
import { AsyncView } from '@/components/AsyncView'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { dateLabel } from '@/lib/format'
import { useBatchCommand, useLedgerCommand, type BatchItem } from '@/lib/command'
import { reportedMoney, WorkspaceReportingDetails } from '@/lib/workspaceReporting'
import {
  countryName, countrySet, loadCountries, loadTrip, loadTrips, tripAssign, tripAssignRetire, tripRetire,
  type ContextRow, type TripDetail, type TripDraft, type TripStatus, type TripSummary, type TripsPage,
} from './api'

const STATUS: Record<TripStatus, string> = { upcoming: 'Upcoming', ongoing: 'Under way', past: 'Past' }

type Saved = (status: string) => void | Promise<void>

/**
 * Trips (v0.7). A transaction joins a trip automatically only when its whole
 * date lies inside the trip and its country is known and listed by the trip;
 * several matching trips wait for a choice. Country is never guessed from
 * currency or merchant: it is set here, explicitly, per transaction.
 */
export function TripsView({ generation, onSaved }: { generation: string; onSaved: Saved }) {
  const [page, setPage] = useState<TripsPage | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState<string | null>(null)
  const [editing, setEditing] = useState<TripSummary | 'new' | null>(null)
  const [codes, setCodes] = useState<string[]>([])

  const load = useCallback(() => {
    setLoading(true); setError('')
    loadTrips().then(setPage).catch(e => setError((e as Error).message)).finally(() => setLoading(false))
  }, [])
  useEffect(() => { load() }, [load, generation])
  useEffect(() => { loadCountries().then(r => setCodes(r.codes)).catch(() => {}) }, [])

  async function saved(status: string) { setEditing(null); await onSaved(status) }

  return <div className="trips-view">
    <AsyncView loading={loading} error={error} data={page} onRetry={load} errorTitle="We couldn’t load your trips.">
      {data => <>
        <WorkspaceReportingDetails data={data}/>
        <div className="row-actions"><Button onClick={() => { setEditing('new'); setSelected(null) }}><Plus size={15}/> New trip</Button></div>
        {editing && <TripForm initial={editing === 'new' ? null : editing} codes={codes} generation={generation} onSaved={saved} onCancel={() => setEditing(null)}/>}
        {data.trips.length === 0 && !editing && <Card className="controls-card"><p className="muted-note">No trips yet. Add one with its dates and the countries it covers; transactions then join it once their country is known.</p></Card>}
        <ul className="trip-list">{data.trips.map(trip => <li key={trip.trip_id}>
          <button className={`trip-card${selected === trip.trip_id ? ' is-active' : ''}`} onClick={() => setSelected(trip.trip_id)} aria-pressed={selected === trip.trip_id}>
            <span className="trip-card-title"><strong>{trip.name}</strong><Badge variant="secondary">{STATUS[trip.status]}</Badge>{trip.review_count > 0 && <Badge variant="outline" className="review-badge">{trip.review_count} to review</Badge>}</span>
            <small>{dateLabel(trip.start_on)} – {dateLabel(trip.end_on)} · {trip.countries.length ? trip.countries.map(countryName).join(', ') : 'No countries listed'}</small>
            <span className="trip-card-totals">{Object.keys(trip.totals_by_currency).length
              ? Object.entries(trip.totals_by_currency).map(([currency, cell]) => <span key={currency}>{reportedMoney(cell, 'net_cost', currency)} <small>({currency} pocket)</small></span>)
              : <small>Nothing assigned yet</small>}</span>
          </button>
        </li>)}</ul>
        <p className="muted-note">{data.lens_note}</p>
        {selected && data.trips.some(t => t.trip_id === selected) && <TripDetailView key={selected} tripId={selected} codes={codes} generation={generation} onSaved={onSaved} onEdit={trip => setEditing(trip)} onClose={() => setSelected(null)}/>}
      </>}
    </AsyncView>
  </div>
}

function TripForm({ initial, codes, generation, onSaved, onCancel }: { initial: TripSummary | null; codes: string[]; generation: string; onSaved: Saved; onCancel: () => void }) {
  const [tripId] = useState(() => initial?.trip_id ?? crypto.randomUUID())
  const [name, setName] = useState(initial?.name ?? '')
  const [start, setStart] = useState(initial?.start_on ?? '')
  const [end, setEnd] = useState(initial?.end_on ?? '')
  const [countries, setCountries] = useState((initial?.countries ?? []).join(', '))
  const [reason, setReason] = useState('')
  const cmd = useLedgerCommand(generation, onSaved)
  const parsed = countries.split(/[\s,]+/).map(c => c.trim().toUpperCase()).filter(Boolean)
  const unknown = codes.length ? parsed.filter(c => !codes.includes(c)) : []
  const draft: TripDraft = { name: name.trim(), start_on: start, end_on: end, countries: [...new Set(parsed)], reason: reason.trim() }
  const ready = Boolean(draft.name && start && end && start <= end && draft.reason && unknown.length === 0)
  function edit(set: (value: string) => void) { return (e: React.ChangeEvent<HTMLInputElement>) => { set(e.target.value); cmd.reset() } }
  return <Card className="controls-card trip-form">
    <div className="panel-title"><div><p className="eyebrow">{initial ? 'EDIT TRIP' : 'NEW TRIP'}</p><h2>{initial ? initial.name : 'Where and when'}</h2></div><Button variant="ghost" size="sm" onClick={onCancel} aria-label="Close trip form"><X size={15}/></Button></div>
    <div className="form-grid">
      <label>Name<input value={name} maxLength={80} onChange={edit(setName)}/></label>
      <label>First day<input type="date" value={start} onChange={edit(setStart)}/></label>
      <label>Last day<input type="date" value={end} onChange={edit(setEnd)}/></label>
      <label>Countries<input value={countries} onChange={edit(setCountries)} placeholder="JP, KR" aria-describedby="trip-countries-help"/></label>
      <label>Reason<input value={reason} onChange={edit(setReason)} placeholder="Why this trip is recorded"/></label>
    </div>
    <p id="trip-countries-help" className="muted-note">Two-letter country codes, separated by commas. A trip with no countries never collects transactions automatically.{parsed.length > 0 && ` ${parsed.map(c => `${c} = ${countryName(c)}`).join(' · ')}`}</p>
    {start && end && start > end && <p className="form-error" role="alert">The last day is before the first day.</p>}
    {unknown.length > 0 && <p className="form-error" role="alert">Not a country code: {unknown.join(', ')}</p>}
    {cmd.previewed && <div className="preview" role="status"><p>{initial ? 'Updates' : 'Creates'} “{draft.name}”, {dateLabel(start)} – {dateLabel(end)}, {draft.countries.length ? draft.countries.join(', ') : 'no countries'}. Transactions are reassigned automatically; manual choices are kept.</p></div>}
    {cmd.error && <p className="form-error" role="alert">{cmd.error}</p>}
    <div className="row-actions"><Button disabled={!ready || cmd.busy} onClick={() => void cmd.act({ kind: 'TRIP_SET', anchors: { trip: tripId }, payload: { ...draft } })}>{cmd.busy ? 'Working…' : cmd.previewed ? 'Save trip' : 'Preview trip'}</Button></div>
  </Card>
}

type Action = 'country' | 'assign' | 'exclude' | 'clear'
const ACTION_LABEL: Record<Action, string> = { country: 'Set country', assign: 'Assign to this trip', exclude: 'Exclude from every trip', clear: 'Return to automatic' }

function TripDetailView({ tripId, codes, generation, onSaved, onEdit, onClose }: { tripId: string; codes: string[]; generation: string; onSaved: Saved; onEdit: (trip: TripSummary) => void; onClose: () => void }) {
  const [detail, setDetail] = useState<TripDetail | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [checked, setChecked] = useState<Set<string>>(new Set())
  const [action, setAction] = useState<Action>('country')
  const [country, setCountry] = useState('')
  const [reason, setReason] = useState('')
  const [reviewed, setReviewed] = useState<BatchItem[] | null>(null)
  const batch = useBatchCommand(generation, async status => { setChecked(new Set()); setReviewed(null); await onSaved(status) })

  const load = useCallback(() => {
    setLoading(true); setError('')
    loadTrip(tripId).then(d => { setDetail(d); if (!country) setCountry(d.trip.countries[0] ?? '') }).catch(e => setError((e as Error).message)).finally(() => setLoading(false))
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tripId])
  useEffect(() => { load() }, [load, generation])

  const rows = useMemo(() => {
    if (!detail) return new Map<string, ContextRow>()
    const all = [...detail.contributors, ...detail.review.missing_country, ...detail.review.ambiguous, ...detail.review.other_country, ...detail.review.excluded]
    return new Map(all.map(r => [r.anchor, r]))
  }, [detail])

  function toggle(anchor: string) {
    setChecked(current => { const next = new Set(current); if (next.has(anchor)) next.delete(anchor); else next.add(anchor); return next })
    setReviewed(null); batch.invalidate()
  }

  function review() {
    const why = reason.trim()
    if (!why || checked.size === 0) return
    const items = [...checked].map(anchor => action === 'country' ? countrySet(anchor, country || null, why)
      : action === 'assign' ? tripAssign(anchor, tripId, why)
      : action === 'exclude' ? tripAssign(anchor, null, why)
      : tripAssignRetire(anchor, why))
    setReviewed(items)
  }

  async function retireTrip() {
    if (!detail) return
    const why = reason.trim() || `Removed trip ${detail.trip.name}`
    const manual = detail.manual_assignments.map(r => tripAssignRetire(r.anchor, why))
    await batch.submit([...manual, tripRetire(tripId, why)], why)
    onClose()
  }

  return <Card className="controls-card trip-detail">
    <AsyncView loading={loading} error={error} data={detail} onRetry={load} errorTitle="We couldn’t load this trip.">
      {d => <>
        <WorkspaceReportingDetails data={d}/>
        <div className="panel-title"><div><p className="eyebrow"><MapPinned size={12}/> TRIP · {dateLabel(d.trip.start_on)} – {dateLabel(d.trip.end_on)} · {STATUS[d.trip.status].toUpperCase()}</p><h2>{d.trip.name}</h2><p className="muted-note">{d.trip.countries.length ? d.trip.countries.map(countryName).join(', ') : 'No countries listed: nothing joins automatically.'}</p></div>
          <div className="row-actions"><Button variant="outline" size="sm" onClick={() => onEdit(d.trip)}>Edit</Button><Button variant="ghost" size="sm" onClick={onClose} aria-label="Close trip"><X size={15}/></Button></div></div>
        {Object.keys(d.trip.totals_by_currency).length > 0 ? <dl className="baseline-metrics">{Object.entries(d.trip.totals_by_currency).map(([currency, cell]) => <div key={currency}><dt>{currency} pocket · cost / refunds / net</dt><dd>{reportedMoney(cell, 'gross_cost', currency)} · {reportedMoney(cell, 'refunds', currency)} · <strong>{reportedMoney(cell, 'net_cost', currency)}</strong></dd></div>)}</dl>
          : <p className="muted-note">Nothing is assigned to this trip yet.</p>}
        <p className="muted-note">{d.lens_note}</p>
        <section className="trip-budget" aria-label="Budget"><h3>Budget</h3><p className="muted-note">No budget for this trip yet.</p></section>
        {d.by_category.length > 0 && <div className="table-scroll"><table className="pocket-table"><thead><tr><th>Category</th><th>Pocket</th><th>Cost</th></tr></thead>
          <tbody>{d.by_category.map((row, i) => <tr key={i}><td>{row.category_key}</td><td>{row.currency}</td><td>{reportedMoney(row, 'cost_amount', row.currency)}</td></tr>)}</tbody></table></div>}
        <RowSection title="In this trip" rows={d.contributors} checked={checked} onToggle={toggle} empty="No transactions yet."/>
        <RowSection title="Inside the dates, country unknown" rows={d.review.missing_country} checked={checked} onToggle={toggle} empty="None." note="Set their country to let them join automatically, or assign them by hand."/>
        <RowSection title="Inside the dates of more than one trip" rows={d.review.ambiguous} checked={checked} onToggle={toggle} empty="None." note="Choose the trip by hand; there is no automatic tie-break."/>
        <RowSection title="Inside the dates, another country" rows={d.review.other_country} checked={checked} onToggle={toggle} empty="None."/>
        <RowSection title="Excluded by hand" rows={d.review.excluded} checked={checked} onToggle={toggle} empty="None."/>
        <div className="trip-actions" role="group" aria-label="Change the selected transactions">
          <span>{checked.size} selected</span>
          <select aria-label="Action" value={action} onChange={e => { setAction(e.target.value as Action); setReviewed(null); batch.invalidate() }}>{(Object.keys(ACTION_LABEL) as Action[]).map(a => <option key={a} value={a}>{ACTION_LABEL[a]}</option>)}</select>
          {action === 'country' && <select aria-label="Country" value={country} onChange={e => { setCountry(e.target.value); setReviewed(null); batch.invalidate() }}>
            <option value="">Unknown (explicit)</option>
            {[...new Set([...d.trip.countries, ...codes])].map(code => <option key={code} value={code}>{code} · {countryName(code)}</option>)}
          </select>}
          <input aria-label="Reason" value={reason} onChange={e => { setReason(e.target.value); setReviewed(null); batch.invalidate() }} placeholder="Reason"/>
          <Button variant="outline" disabled={checked.size === 0 || !reason.trim()} onClick={review}>Review changes</Button>
        </div>
        {reviewed && <div className="preview" role="status"><p>{ACTION_LABEL[action]}{action === 'country' ? ` to ${country ? `${country} (${countryName(country)})` : 'unknown'}` : ''} on {reviewed.length} transaction{reviewed.length === 1 ? '' : 's'}: {[...checked].map(a => rows.get(a)?.merchant).filter(Boolean).slice(0, 5).join(', ')}{checked.size > 5 ? '…' : ''}.</p>
          <Button disabled={batch.busy} onClick={() => void batch.submit(reviewed, reason.trim())}>{batch.busy ? 'Saving…' : `Save ${reviewed.length} change${reviewed.length === 1 ? '' : 's'}`}</Button></div>}
        {batch.error && <div className="alert error" role="alert"><p>{batch.error}</p>{batch.submitted && <Button variant="outline" onClick={() => void batch.retry()}>Retry same save</Button>}</div>}
        <details className="trip-retire"><summary>Remove this trip</summary><p className="muted-note">Its {d.manual_assignments.length} manual assignment(s) are retired in the same save{d.manual_assignments.some(m => !m.counted) && ', including ones no longer counted as trip cost'}; automatic ones simply stop matching. Transactions and countries are kept.</p><Button variant="outline" disabled={batch.busy} onClick={() => void retireTrip()}>Remove trip</Button></details>
      </>}
    </AsyncView>
  </Card>
}

// Amounts stay exact decimal text from the server; this only reads whether one is zero.
const isZero = (amount: string) => /^[+-]?0*(\.0*)?$/.test(amount)

const BASIS: Record<string, string> = { automatic: 'Automatic', manual: 'Chosen by hand', excluded: 'Excluded', ambiguous: 'Several trips', none: 'Not in a trip' }

function RowSection({ title, rows, checked, onToggle, empty, note }: { title: string; rows: ContextRow[]; checked: Set<string>; onToggle: (anchor: string) => void; empty: string; note?: string }) {
  return <section className="trip-rows">
    <h3>{title} <small>({rows.length})</small></h3>
    {note && rows.length > 0 && <p className="muted-note">{note}</p>}
    {rows.length === 0 ? <p className="muted-note">{empty}</p> : <div className="table-scroll"><table className="pocket-table">
      <thead><tr><th><span className="sr-only">Select</span></th><th>Date</th><th>Merchant</th><th>Country</th><th>Basis</th><th>Amount</th></tr></thead>
      <tbody>{rows.map(row => <tr key={row.anchor}>
        <td><input type="checkbox" aria-label={`Select ${row.merchant} on ${row.on}`} checked={checked.has(row.anchor)} onChange={() => onToggle(row.anchor)}/></td>
        <td>{dateLabel(row.on)}</td>
        <td>{row.merchant}<small>{row.category_key}</small></td>
        <td>{row.country ? `${row.country} · ${countryName(row.country)}` : row.country_basis === 'operator' ? 'Unknown (set)' : 'Unknown'}</td>
        <td>{BASIS[row.assignment]}{row.outside_window && <small>outside the trip dates</small>}</td>
        <td>{!isZero(row.cost_amount) && reportedMoney(row, 'cost_amount', row.currency)}{!isZero(row.refund_amount) && <small>refund {reportedMoney(row, 'refund_amount', row.currency)}</small>}</td>
      </tr>)}</tbody></table></div>}
  </section>
}
