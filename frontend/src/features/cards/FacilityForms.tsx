import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { useLedgerCommand } from '@/lib/command'
import { dateLabel } from '@/lib/format'
import type { CardAccount, CardDetail } from './api'

type Saved = (status: string) => void | Promise<void>
type Draft = { kind: string; anchors: Record<string, string>; payload: Record<string, unknown> }

/** One reviewed write: preview, then save. Shows what will be recorded first. */
function Confirm({ draft, ready, generation, onSaved, summary, label }: {
  draft: Draft; ready: boolean; generation: string; onSaved: Saved; summary: string; label: string
}) {
  const cmd = useLedgerCommand(generation, onSaved)
  return <>
    {cmd.previewed && <p className="muted-note" role="status">{summary}</p>}
    {cmd.error && <p className="form-error" role="alert">{cmd.error}</p>}
    <div className="row-actions">
      <Button size="sm" disabled={!ready || cmd.busy} onClick={() => void cmd.act(draft)}>
        {cmd.busy ? 'Working…' : cmd.previewed ? label : `Preview: ${label.toLowerCase()}`}
      </Button>
    </div>
  </>
}

/**
 * A shared credit facility: the card accounts that draw on one limit. Member
 * accounts must be card accounts, and an account belongs to one facility.
 */
export function FacilityForm({ detail, cards, generation, onSaved, onCancel }: {
  detail: CardDetail; cards: CardAccount[]; generation: string; onSaved: Saved; onCancel: () => void
}) {
  const existing = detail.facility
  const [facilityId] = useState(() => existing?.facility_id ?? crypto.randomUUID())
  const [label, setLabel] = useState(existing?.label ?? detail.account.display_name)
  const [currency, setCurrency] = useState(existing?.currency ?? detail.latest_statement?.closing_liability?.currency ?? 'SGD')
  const [members, setMembers] = useState<string[]>(existing?.account_ids?.length ? existing.account_ids : [detail.account.account_id])
  const [reason, setReason] = useState('')
  const clean = currency.trim().toUpperCase()
  const ready = Boolean(label.trim() && /^[A-Z]{3}$/.test(clean) && members.length && reason.trim())
  const names = cards.filter(c => members.includes(c.account_id)).map(c => c.display_name)
  return <div className="card-form">
    <h3>{existing ? `Edit facility · ${existing.label}` : 'Set up the credit facility'}</h3>
    <p className="muted-note">A facility is the credit line one or more cards share. Its limit is recorded separately, as a dated observation.</p>
    <div className="form-grid">
      <label>Facility name<input value={label} maxLength={80} onChange={e => setLabel(e.target.value)}/></label>
      <label>Currency<input value={currency} maxLength={3} onChange={e => setCurrency(e.target.value)}/></label>
      <label>Reason<input value={reason} placeholder="Why record this" onChange={e => setReason(e.target.value)}/></label>
    </div>
    <fieldset className="card-members"><legend>Cards that share this limit</legend>
      {cards.map(card => <label key={card.account_id}><input type="checkbox" checked={members.includes(card.account_id)}
        onChange={() => setMembers(m => m.includes(card.account_id) ? m.filter(a => a !== card.account_id) : [...m, card.account_id])}/> {card.display_name}</label>)}
    </fieldset>
    <Confirm draft={{ kind: 'CARD_FACILITY_SET', anchors: { facility: facilityId }, payload: { label: label.trim(), account_ids: members, currency: clean, reason: reason.trim() } }}
      ready={ready} generation={generation} onSaved={onSaved} label="Save facility"
      summary={`${existing ? 'Updates' : 'Creates'} “${label.trim()}” in ${clean}, shared by ${names.join(', ')}.`}/>
    <Button variant="ghost" size="sm" onClick={onCancel}>Cancel</Button>
  </div>
}

/** A credit limit as observed on a date (from a letter, the app, a statement). */
export function LimitForm({ detail, generation, onSaved, onCancel }: {
  detail: CardDetail; generation: string; onSaved: Saved; onCancel: () => void
}) {
  const facility = detail.facility!
  const [observationId] = useState(() => crypto.randomUUID())
  const [observedOn, setObservedOn] = useState('')
  const [limit, setLimit] = useState('')
  const [source, setSource] = useState('')
  const [reason, setReason] = useState('')
  const amountOk = /^\d+(\.\d{1,2})?$/.test(limit.trim()) && Number(limit) > 0
  const ready = Boolean(observedOn && amountOk && reason.trim())
  return <div className="card-form">
    <h3>Record the credit limit</h3>
    <p className="muted-note">The limit on {facility.label}, as you saw it on a given day. Earlier observations are kept; the latest one is used.</p>
    <div className="form-grid">
      <label>Seen on<input type="date" value={observedOn} onChange={e => setObservedOn(e.target.value)}/></label>
      <label>Limit ({facility.currency})<input inputMode="decimal" value={limit} placeholder="5000.00" onChange={e => setLimit(e.target.value)}/></label>
      <label>Where you saw it (optional)<input value={source} placeholder="Bank app, letter…" onChange={e => setSource(e.target.value)}/></label>
      <label>Reason<input value={reason} placeholder="Why record this" onChange={e => setReason(e.target.value)}/></label>
    </div>
    {limit && !amountOk && <p className="form-error" role="alert">Enter a positive amount, like 5000 or 5000.00.</p>}
    <Confirm draft={{ kind: 'CARD_LIMIT_OBSERVATION_SET', anchors: { facility: facility.facility_id, observation: observationId },
      payload: { observed_on: observedOn, limit: limit.trim(), source_ref: source.trim() || null, reason: reason.trim() } }}
      ready={ready} generation={generation} onSaved={onSaved} label="Save limit"
      summary={`Records a ${facility.currency} ${limit.trim()} limit on ${facility.label}, seen ${observedOn ? dateLabel(observedOn) : ''}.`}/>
    <Button variant="ghost" size="sm" onClick={onCancel}>Cancel</Button>
  </div>
}

/** Remove the latest limit observation or the facility itself, with a reason. */
export function RetireForm({ detail, target, generation, onSaved, onCancel }: {
  detail: CardDetail; target: 'limit' | 'facility'; generation: string; onSaved: Saved; onCancel: () => void
}) {
  const facility = detail.facility!
  const observation = detail.limit_observation
  const [reason, setReason] = useState('')
  const draft: Draft = target === 'limit' && observation
    ? { kind: 'CARD_LIMIT_OBSERVATION_RETIRE', anchors: { facility: facility.facility_id, observation: observation.observation_id }, payload: { reason: reason.trim() } }
    : { kind: 'CARD_FACILITY_RETIRE', anchors: { facility: facility.facility_id }, payload: { reason: reason.trim() } }
  return <div className="card-form">
    <h3>{target === 'limit' ? 'Remove the recorded limit' : `Remove facility · ${facility.label}`}</h3>
    {target === 'facility' && observation && <p className="muted-note">Remove its recorded limits first; a facility with limits can’t be removed.</p>}
    <div className="form-grid"><label>Reason<input value={reason} onChange={e => setReason(e.target.value)}/></label></div>
    <Confirm draft={draft} ready={Boolean(reason.trim())} generation={generation} onSaved={onSaved}
      label={target === 'limit' ? 'Remove limit' : 'Remove facility'}
      summary={target === 'limit' && observation
        ? `Removes the ${observation.limit.currency ?? ''} ${observation.limit.value} limit seen ${dateLabel(observation.observed_on)}. Statements are not touched.`
        : `Removes the facility “${facility.label}”. Statements are not touched.`}/>
    <Button variant="ghost" size="sm" onClick={onCancel}>Cancel</Button>
  </div>
}

/**
 * How this card is repaid, when its statements can't show it. Only one rule
 * exists: each statement paid in full by its due date. With it, the forecast
 * settles the latest statement's balance on its due date (which already
 * carries anything left unpaid before it).
 */
export function RepaymentPlanForm({ detail, generation, onSaved, onCancel }: {
  detail: CardDetail; generation: string; onSaved: Saved; onCancel: () => void
}) {
  const current = detail.repayment_plan
  const [reason, setReason] = useState('')
  const account = detail.account.account_id
  const draft: Draft = current
    ? { kind: 'CARD_REPAYMENT_PLAN_RETIRE', anchors: { account }, payload: { reason: reason.trim() } }
    : { kind: 'CARD_REPAYMENT_PLAN_SET', anchors: { account }, payload: { rule: 'pay_in_full', reason: reason.trim() } }
  return <div className="card-form">
    <h3>{current ? 'Stop using this repayment plan' : 'I pay each statement in full by its due date'}</h3>
    <p className="muted-note">{current
      ? 'The forecast will go back to leaving this card’s repayments out until they can be read from your statements.'
      : 'From now on, the forecast pays this card’s latest statement balance on its due date (or tomorrow, if that date has passed). What your statements show is still listed separately.'}</p>
    <div className="form-grid"><label>Reason<input value={reason} placeholder={current ? 'Why stop' : 'e.g. I clear it every month'} onChange={e => setReason(e.target.value)}/></label></div>
    <Confirm draft={draft} ready={Boolean(reason.trim())} generation={generation} onSaved={onSaved}
      label={current ? 'Remove plan' : 'Save plan'}
      summary={current
        ? `Removes the “pay in full” plan for ${detail.account.display_name}.`
        : `Records that ${detail.account.display_name} is paid in full by each due date. Nothing about your statements changes.`}/>
    <Button variant="ghost" size="sm" onClick={onCancel}>Cancel</Button>
  </div>
}
