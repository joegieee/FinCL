import { useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { ApiError, money } from '@/lib/api'
import { dateLabel } from '@/lib/format'
import { DraftEffect, RecloseControl, useDebtDraft } from './Draft'
import { EventForm } from './EventForm'
import { aroundClosed, kindLabel, loadDebtEvent, type DebtDetail, type DebtEvent, type DebtEventRead } from './api'

/** Correct a recorded event: the event form, started from the event's own values. */
export function CorrectEventForm({ detail, event, generation, categories, onSaved, onCancel }: {
  detail: DebtDetail
  event: DebtEvent
  generation: string
  categories: string[]
  onSaved: (status: string) => void | Promise<void>
  onCancel: () => void
}) {
  const [read, setRead] = useState<DebtEventRead | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let live = true
    loadDebtEvent(detail.debt.debt_id, event.event_id, generation)
      .then((value) => { if (live) setRead(value) })
      .catch((e) => { if (live) setError(e instanceof ApiError ? e.message : 'Could not load that event.') })
    return () => { live = false }
  }, [detail.debt.debt_id, event.event_id, generation])

  if (error) return <div className="debt-event-form"><p className="debt-event-error" role="alert">{error}</p>
    <div className="debt-event-actions"><Button variant="ghost" size="sm" onClick={onCancel}>Back</Button></div></div>
  if (!read) return <p className="muted-note">Loading the event…</p>
  return <EventForm detail={detail} generation={generation} categories={categories} replacing={read}
    onSaved={onSaved} onCancel={onCancel} />
}

/** Remove a recorded event, after seeing what it does to every later balance. */
export function RemoveEventForm({ detail, event, generation, onSaved, onCancel }: {
  detail: DebtDetail
  event: DebtEvent
  generation: string
  onSaved: (status: string) => void | Promise<void>
  onCancel: () => void
}) {
  const debt = detail.debt
  const [reason, setReason] = useState('')
  const [recloseOn, setRecloseOn] = useState<string | null>(debt.closed_on)
  const [problem, setProblem] = useState('')
  const draft = useDebtDraft(generation, onSaved)

  function edit(change: () => void) { change(); setProblem(''); draft.invalidate() }

  function submit() {
    if (draft.preview) { void draft.save(reason.trim()); return }
    if (!reason.trim()) { setProblem('A reason is required.'); return }
    void draft.check(aroundClosed(debt, [
      { kind: 'PERSONAL_DEBT_EVENT_RETIRE', anchors: { debt: debt.debt_id, event: event.event_id }, payload: { reason: reason.trim() } },
    ], { reopenOn: detail.as_of, recloseOn, reason: reason.trim() }))
  }

  return <div className="debt-event-form">
    <div className="debt-event-title">
      <h3>Remove an event · {debt.counterparty_label}</h3>
      <span>
        {kindLabel(event.event_kind)} of {money(event.principal.value, debt.currency)} on {dateLabel(event.effective_on)}
      </span>
    </div>
    <p className="muted-note">
      The event stops counting towards the balance. Its source transactions return to their ordinary classification
      unless another record explains them. The decision history keeps what was recorded and why it was removed.
    </p>
    <label className="debt-event-reason">
      Reason
      <textarea aria-label="Reason" rows={2} value={reason} disabled={draft.busy}
        placeholder="Why is this event wrong?" onChange={(e) => edit(() => setReason(e.target.value))} />
    </label>
    {debt.status === 'CLOSED' && debt.closed_on
      ? <RecloseControl closedOn={debt.closed_on} value={recloseOn} onChange={(v) => edit(() => setRecloseOn(v))} disabled={draft.busy} />
      : null}
    {draft.preview ? <DraftEffect preview={draft.preview} /> : null}
    {problem || draft.error ? <p className="debt-event-error" role="alert">{problem || draft.error}</p> : null}
    <div className="debt-event-actions">
      <Button variant="ghost" size="sm" onClick={onCancel} disabled={draft.busy}>Cancel</Button>
      <Button size="sm" onClick={submit} disabled={draft.busy}>
        {draft.busy ? 'Working…' : draft.preview ? (draft.retrying ? 'Retry save' : 'Remove event') : 'Check removal'}
      </Button>
    </div>
  </div>
}
