import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { ApiError, money } from '@/lib/api'
import { dateLabel } from '@/lib/format'
import { useDebtDraft } from './Draft'
import {
  PLAN_STATE_LABEL, cadenceSchedule, loadCommitmentSchedule,
  type DebtDetail, type DraftItem, type OverlappingCommitment, type PlanCadence, type PlanState,
} from './api'

export const planTitle = (direction: 'PAYABLE' | 'RECEIVABLE') => (direction === 'PAYABLE' ? 'Repayment plan' : 'Expected repayments')

function PlanTable({ plan, currency }: { plan: PlanState; currency: string }) {
  return <>
    <table className="debt-history debt-plan-rows">
      <thead><tr><th>Due</th><th>Amount</th><th>Paid</th><th>Remaining</th><th>State</th></tr></thead>
      <tbody>
        {plan.rows.map(row => <tr key={row.schedule_index} className={row.state === 'overdue' ? 'is-changed' : undefined}>
          <td>{dateLabel(row.due_on)}</td>
          <td>{money(row.amount, currency)}</td>
          <td>{money(row.paid, currency)}</td>
          <td>{money(row.remaining, currency)}</td>
          <td><span className={row.state === 'overdue' ? 'tag warn' : 'tag'}>{PLAN_STATE_LABEL[row.state]}</span></td>
        </tr>)}
        {plan.unscheduled_remainder && <tr className="is-changed">
          <td>{plan.unscheduled_remainder.due_on ? dateLabel(plan.unscheduled_remainder.due_on) : 'No date'}</td>
          <td colSpan={2}>Not in the plan</td>
          <td>{money(plan.unscheduled_remainder.amount, currency)}</td>
          <td><span className="tag warn">{plan.unscheduled_remainder.overdue ? 'Overdue' : 'Unscheduled'}</span></td>
        </tr>}
      </tbody>
    </table>
    {plan.rows.some(row => row.was_trimmed) && (
      <p className="muted-note">The last rows were cut down because less is owed than the plan asked for.</p>
    )}
    {plan.unscheduled_remainder && (
      <p className="muted-note">
        {money(plan.unscheduled_remainder.amount, currency)} of what is owed is not on the plan
        {plan.unscheduled_remainder.due_on ? ` and falls due on ${dateLabel(plan.unscheduled_remainder.due_on)}.` : ', and the debt has no due date, so the forecast cannot place it.'}
      </p>
    )}
  </>
}

/** The plan as it would be once saved: rows against what is owed. */
function PlanEffect({ plan, currency }: { plan: PlanState | null | undefined; currency: string }) {
  if (!plan) return <p className="muted-note" role="status">The debt goes back to one lump sum.</p>
  return <div className="debt-draft-effect" role="status">
    <p>The plan covers {money(plan.scheduled_remaining, currency)} of the {money(plan.remaining_principal, currency)} still owed.</p>
    <PlanTable plan={plan} currency={currency} />
  </div>
}

type Row = { due_on: string; amount: string }

function PlanEditor({ detail, generation, onSaved, onCancel }: {
  detail: DebtDetail; generation: string; onSaved: (status: string) => void | Promise<void>; onCancel: () => void
}) {
  const debt = detail.debt
  const [mode, setMode] = useState<'repeating' | 'dates'>('repeating')
  const [cadence, setCadence] = useState<PlanCadence>('monthly')
  const [amount, setAmount] = useState('')
  const [start, setStart] = useState('')
  const [untilPaid, setUntilPaid] = useState(true)
  const [count, setCount] = useState('')
  const [rows, setRows] = useState<Row[]>([{ due_on: '', amount: '' }, { due_on: '', amount: '' }])
  const [reason, setReason] = useState('')
  const [problem, setProblem] = useState('')
  const draft = useDebtDraft(generation, onSaved)

  function edit(change: () => void) { change(); setProblem(''); draft.invalidate() }
  const setRow = (index: number, change: Partial<Row>) => edit(() => setRows(rows.map((row, i) => (i === index ? { ...row, ...change } : row))))

  function item(): DraftItem | string {
    if (!reason.trim()) return 'A reason is required.'
    let plan: Record<string, unknown>
    let first: string
    if (mode === 'repeating') {
      if (!amount.trim() || !start) return 'Enter the amount of each payment and the date of the first.'
      if (!untilPaid && !/^\d+$/.test(count)) return 'Enter how many payments, or choose until paid.'
      first = start
      plan = { kind: 'series', schedule: cadenceSchedule(cadence, start), amount: amount.trim(), count: untilPaid ? null : Number(count) }
    } else {
      if (rows.some(row => !row.due_on || !row.amount.trim())) return 'Every row needs a date and an amount.'
      first = rows[0].due_on
      plan = { kind: 'rows', rows: rows.map(row => ({ due_on: row.due_on, amount: row.amount.trim() })) }
    }
    // Repayments recorded before today do not pay a plan that starts later.
    const from = [debt.opened_on, first < detail.as_of ? first : detail.as_of].sort().at(-1) as string
    return { kind: 'PERSONAL_DEBT_SCHEDULE_SET', anchors: { debt: debt.debt_id }, payload: { effective_from: from, plan, reason: reason.trim() } }
  }

  function submit() {
    if (draft.preview) { void draft.save(reason.trim()); return }
    const next = item()
    if (typeof next === 'string') { setProblem(next); return }
    void draft.check([next])
  }

  return <div className="debt-event-form">
    <div className="debt-event-title"><h3>{debt.schedule ? 'Change the plan' : 'Add a plan'} · {debt.counterparty_label}</h3></div>
    <div className="debt-lifecycle-row" role="group" aria-label="How the plan is written">
      <Button size="sm" variant={mode === 'repeating' ? 'default' : 'outline'} disabled={draft.busy} onClick={() => edit(() => setMode('repeating'))}>Repeating</Button>
      <Button size="sm" variant={mode === 'dates' ? 'default' : 'outline'} disabled={draft.busy} onClick={() => edit(() => setMode('dates'))}>Dates and amounts</Button>
    </div>
    {mode === 'repeating' ? <div className="debt-lifecycle-row">
      <select aria-label="Cadence" value={cadence} disabled={draft.busy} onChange={e => edit(() => setCadence(e.target.value as PlanCadence))}>
        <option value="monthly">Every month</option><option value="fortnightly">Every 2 weeks</option><option value="weekly">Every week</option>
      </select>
      <input aria-label="Payment amount" placeholder="Each payment" inputMode="decimal" value={amount} disabled={draft.busy} onChange={e => edit(() => setAmount(e.target.value))} />
      <input aria-label="First payment on" type="date" value={start} disabled={draft.busy} onChange={e => edit(() => setStart(e.target.value))} />
      <label className="debt-event-check"><input type="checkbox" checked={untilPaid} disabled={draft.busy} onChange={e => edit(() => setUntilPaid(e.target.checked))} /><span>Until paid</span></label>
      {!untilPaid && <input aria-label="Number of payments" placeholder="How many" inputMode="numeric" value={count} disabled={draft.busy} onChange={e => edit(() => setCount(e.target.value))} />}
    </div> : <div>
      {rows.map((row, index) => <div className="debt-lifecycle-row" key={index}>
        <input aria-label={`Row ${index + 1} date`} type="date" value={row.due_on} disabled={draft.busy} onChange={e => setRow(index, { due_on: e.target.value })} />
        <input aria-label={`Row ${index + 1} amount`} placeholder="Amount" inputMode="decimal" value={row.amount} disabled={draft.busy} onChange={e => setRow(index, { amount: e.target.value })} />
        <Button size="sm" variant="ghost" disabled={draft.busy || rows.length === 1} aria-label={`Remove row ${index + 1}`}
          onClick={() => edit(() => setRows(rows.filter((_, i) => i !== index)))}>Remove</Button>
      </div>)}
      <Button size="sm" variant="outline" disabled={draft.busy} onClick={() => edit(() => setRows([...rows, { due_on: '', amount: '' }]))}>Add a row</Button>
    </div>}
    <label className="debt-event-reason">Reason
      <textarea aria-label="Plan reason" rows={2} value={reason} disabled={draft.busy} placeholder="What was agreed?" onChange={e => edit(() => setReason(e.target.value))} />
    </label>
    {draft.preview ? <PlanEffect plan={draft.preview.debts[0]?.schedule?.after} currency={debt.currency} /> : null}
    {problem || draft.error ? <p className="debt-event-error" role="alert">{problem || draft.error}</p> : null}
    <div className="debt-event-actions">
      <Button variant="ghost" size="sm" onClick={onCancel} disabled={draft.busy}>Cancel</Button>
      <Button size="sm" onClick={submit} disabled={draft.busy}>
        {draft.busy ? 'Working…' : draft.preview ? (draft.retrying ? 'Retry save' : 'Save plan') : 'Check plan'}
      </Button>
    </div>
  </div>
}

function RemovePlan({ detail, generation, onSaved, onCancel }: {
  detail: DebtDetail; generation: string; onSaved: (status: string) => void | Promise<void>; onCancel: () => void
}) {
  const debt = detail.debt
  const [reason, setReason] = useState('')
  const [problem, setProblem] = useState('')
  const draft = useDebtDraft(generation, onSaved)
  function submit() {
    if (draft.preview) { void draft.save(reason.trim()); return }
    if (!reason.trim()) { setProblem('A reason is required.'); return }
    void draft.check([{ kind: 'PERSONAL_DEBT_SCHEDULE_RETIRE', anchors: { debt: debt.debt_id }, payload: { reason: reason.trim() } }])
  }
  return <div className="debt-event-form">
    <div className="debt-event-title"><h3>Remove the plan · {debt.counterparty_label}</h3></div>
    <label className="debt-event-reason">Reason
      <textarea aria-label="Plan reason" rows={2} value={reason} disabled={draft.busy}
        onChange={e => { setReason(e.target.value); setProblem(''); draft.invalidate() }} />
    </label>
    {draft.preview ? <PlanEffect plan={draft.preview.debts[0]?.schedule?.after} currency={debt.currency} /> : null}
    {problem || draft.error ? <p className="debt-event-error" role="alert">{problem || draft.error}</p> : null}
    <div className="debt-event-actions">
      <Button variant="ghost" size="sm" onClick={onCancel} disabled={draft.busy}>Cancel</Button>
      <Button size="sm" onClick={submit} disabled={draft.busy}>
        {draft.busy ? 'Working…' : draft.preview ? (draft.retrying ? 'Retry save' : 'Remove plan') : 'Check removal'}
      </Button>
    </div>
  </div>
}

/** Turn a recurring commitment that repeats this debt's repayments into the
 * debt's plan, ending the commitment in the same record. */
function UseCommitment({ detail, commitment, generation, onSaved, onCancel }: {
  detail: DebtDetail; commitment: OverlappingCommitment; generation: string
  onSaved: (status: string) => void | Promise<void>; onCancel: () => void
}) {
  const debt = detail.debt
  const [reason, setReason] = useState('')
  const [problem, setProblem] = useState('')
  const draft = useDebtDraft(generation, onSaved)
  async function submit() {
    if (draft.preview) { await draft.save(reason.trim()); return }
    if (!reason.trim()) { setProblem('A reason is required.'); return }
    try {
      const { items } = await loadCommitmentSchedule(debt.debt_id, commitment.commitment, generation)
      await draft.check(items.map(item => ({ ...item, payload: { ...item.payload, reason: reason.trim() } })))
    } catch (e) {
      setProblem(e instanceof ApiError ? e.message : 'Could not build the plan.')
    }
  }
  return <div className="debt-event-form">
    <div className="debt-event-title"><h3>Use “{commitment.name}” as the plan · {debt.counterparty_label}</h3></div>
    <p className="muted-note">
      The commitment’s remaining dates, at their expected amounts, become this debt’s repayment plan, and the commitment ends.
      Payments already linked to it stay out of your usual spending.
    </p>
    <label className="debt-event-reason">Reason
      <textarea aria-label="Plan reason" rows={2} value={reason} disabled={draft.busy}
        onChange={e => { setReason(e.target.value); setProblem(''); draft.invalidate() }} />
    </label>
    {draft.preview ? <PlanEffect plan={draft.preview.debts[0]?.schedule?.after} currency={debt.currency} /> : null}
    {problem || draft.error ? <p className="debt-event-error" role="alert">{problem || draft.error}</p> : null}
    <div className="debt-event-actions">
      <Button variant="ghost" size="sm" onClick={onCancel} disabled={draft.busy}>Cancel</Button>
      <Button size="sm" onClick={() => void submit()} disabled={draft.busy}>
        {draft.busy ? 'Working…' : draft.preview ? (draft.retrying ? 'Retry save' : 'Use as the plan') : 'Check plan'}
      </Button>
    </div>
  </div>
}

type Panel = { kind: 'edit' } | { kind: 'remove' } | { kind: 'commitment'; commitment: OverlappingCommitment } | null

/** The debt's repayment plan: its rows against the repayments recorded, and
 * the ways to write, change, remove or take one from a recurring commitment. */
export function RepaymentPlan({ detail, generation, onSaved, onBusy }: {
  detail: DebtDetail; generation: string; onSaved: (status: string) => void | Promise<void>
  /** Told whether a plan form is open, so the page can hold its other controls. */
  onBusy?: (busy: boolean) => void
}) {
  const debt = detail.debt
  const [panel, setPanelState] = useState<Panel>(null)
  const setPanel = (next: Panel) => { setPanelState(next); onBusy?.(next !== null) }
  const plan = debt.schedule
  const open = debt.status === 'OPEN'
  if (!plan && !open) return null
  const done = async (status: string) => { setPanel(null); await onSaved(status) }
  const cancel = () => setPanel(null)
  return <section className="debt-plan" aria-label={planTitle(debt.direction)}>
    <h3>{planTitle(debt.direction)}</h3>
    {plan && plan.overdue_count > 0 && (
      <p className="debt-event-error" role="note">
        {plan.overdue_count === 1 ? 'A payment' : `${plan.overdue_count} payments`} on this plan {plan.overdue_count === 1 ? 'is' : 'are'} past due and not covered by a recorded repayment.
        {debt.direction === 'PAYABLE'
          ? ' Record the repayment if you made it, or correct the plan; until then it is counted on the first day of the forecast.'
          : ' Record what came back, or write it off; until then it is not counted on.'}
      </p>
    )}
    {plan ? <PlanTable plan={plan} currency={debt.currency} />
      : <p className="muted-note">No plan. {debt.direction === 'PAYABLE' ? 'The forecast treats this debt as one lump sum' : 'This money is not counted on'}{debt.due_on ? ` on ${dateLabel(debt.due_on)}.` : ' and, with no due date, cannot place it.'}</p>}
    {panel?.kind === 'edit' && <PlanEditor detail={detail} generation={generation} onSaved={done} onCancel={cancel} />}
    {panel?.kind === 'remove' && <RemovePlan detail={detail} generation={generation} onSaved={done} onCancel={cancel} />}
    {panel?.kind === 'commitment' && <UseCommitment detail={detail} commitment={panel.commitment} generation={generation} onSaved={done} onCancel={cancel} />}
    {open && !panel && <div className="debt-event-actions">
      <Button size="sm" variant="outline" onClick={() => setPanel({ kind: 'edit' })}>{plan ? 'Change the plan' : 'Add a plan'}</Button>
      {plan && <Button size="sm" variant="ghost" onClick={() => setPanel({ kind: 'remove' })}>Remove plan</Button>}
      {debt.overlapping_commitments?.map(c => (
        <Button key={c.commitment} size="sm" variant="outline" onClick={() => setPanel({ kind: 'commitment', commitment: c })}>
          Use this commitment as the debt’s schedule{debt.overlapping_commitments && debt.overlapping_commitments.length > 1 ? ` (${c.name})` : ''}
        </Button>
      ))}
    </div>}
  </section>
}
