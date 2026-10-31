import { WorkspaceReportingDetails, reportedMoney } from '@/lib/workspaceReporting'
import { useEffect, useState } from 'react'
import { ArrowLeft, ChevronRight, Plus, Split } from 'lucide-react'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { ApiError, money } from '@/lib/api'
import { dateLabel } from '@/lib/format'
import { CorrectEventForm, RemoveEventForm } from './CorrectEvent'
import { EventForm } from './EventForm'
import { NewDebtForm } from './NewDebtForm'
import { RepaymentPlan } from './Plan'
import { SplitBillForm } from './SplitBillForm'
import {
  RECONCILIATION_LABEL,
  detailsCommand,
  kindLabel,
  lifecycleCommand,
  loadDebtDetail,
  loadDebts,
  type DebtCard,
  type DebtDetail,
  type DebtEvent,
  type DebtsPage,
} from './api'

function DebtRow({ debt, onOpen }: { debt: DebtCard; onOpen: (id: string) => void }) {
  return (
    <button className="debt-row" onClick={() => onOpen(debt.debt_id)}>
      <span className="debt-row-main">
        <strong>{debt.counterparty_label}</strong>
        <small>
          {debt.reconciliation_status === 'opening_balance_only'
            ? 'Opening balance'
            : RECONCILIATION_LABEL[debt.reconciliation_status] ?? debt.reconciliation_status}
          {debt.plan?.next_due
            ? ` · next payment ${dateLabel(debt.plan.next_due)}${debt.plan.overdue_count ? ' · overdue' : ''}`
            : debt.due_on ? ` · due ${dateLabel(debt.due_on)}` : debt.direction === 'PAYABLE' && debt.status === 'OPEN' ? ' · no due date — not in the forecast' : ''}
        </small>
      </span>
      <span className="debt-row-amount">
        {reportedMoney(debt.remaining_principal, 'value', debt.currency)}
      </span>
      <ChevronRight size={15} />
    </button>
  )
}

function DebtList({
  title, caption, debts, onOpen,
}: { title: string; caption: string; debts: DebtCard[]; onOpen: (id: string) => void }) {
  const byCurrency = new Map<string, DebtCard[]>()
  for (const d of debts) byCurrency.set(d.currency, [...(byCurrency.get(d.currency) ?? []), d])
  return (
    <Card className="debt-list">
      <header><h3>{title}</h3><p>{caption}</p></header>
      {debts.length === 0 ? (
        <p className="debt-empty">Nothing recorded.</p>
      ) : (
        [...byCurrency.entries()].map(([currency, group]) => (
          <div key={currency} className="debt-currency-group">
            <div className="debt-currency-head">{currency}</div>
            {group.map((d) => <DebtRow key={d.debt_id} debt={d} onOpen={onOpen} />)}
          </div>
        ))
      )}
    </Card>
  )
}

/** Name and due date. A debt with no due date is not in any day of the
 * forecast, so "Will I have enough" can only flag it, not place it. */
function DetailsControls({ detail, generation, onSaved }: {
  detail: DebtDetail; generation: string; onSaved: (status: string) => void | Promise<void>
}) {
  const debt = detail.debt
  const [open, setOpen] = useState(!debt.due_on && debt.direction === 'PAYABLE' && debt.status === 'OPEN')
  const [label, setLabel] = useState(debt.counterparty_label)
  const [dueOn, setDueOn] = useState(debt.due_on ?? '')
  const [reason, setReason] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const changed = label.trim() !== debt.counterparty_label || (dueOn || null) !== (debt.due_on ?? null)

  async function submit() {
    if (busy) return
    if (!label.trim() || !reason.trim()) { setError('A name and a reason are required.'); return }
    setBusy(true); setError('')
    try {
      const result = await detailsCommand({ debtId: debt.debt_id, generation, counterpartyLabel: label.trim(), dueOn: dueOn || null, reason: reason.trim() })
      await onSaved(result.status)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not update the debt.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <details className="debt-lifecycle" open={open} onToggle={e => setOpen(e.currentTarget.open)}>
      <summary>{debt.due_on ? 'Change name or due date' : 'Give this debt a due date'}</summary>
      {!debt.due_on && debt.direction === 'PAYABLE' && (
        <p className="muted-note">Without a due date this debt isn’t in your forecast, so “Will I have enough” can only warn about it. Add the date you expect to repay it.</p>
      )}
      <div className="debt-lifecycle-row">
        <input aria-label="Debt name" value={label} maxLength={200} onChange={e => setLabel(e.target.value)} disabled={busy} />
        <input aria-label="Due date" type="date" value={dueOn} min={debt.opened_on} onChange={e => setDueOn(e.target.value)} disabled={busy} />
        <input aria-label="Details reason" placeholder="Reason" value={reason} onChange={e => setReason(e.target.value)} disabled={busy} />
        <Button size="sm" onClick={submit} disabled={busy || !changed}>{busy ? 'Saving…' : 'Save'}</Button>
      </div>
      {error && <p className="debt-event-error" role="alert">{error}</p>}
    </details>
  )
}

function LifecycleControls({
  detail, generation, onSaved,
}: {
  detail: DebtDetail
  generation: string
  onSaved: (status: string) => void | Promise<void>
}) {
  const debt = detail.debt
  const closing = debt.status === 'OPEN'
  const [effectiveOn, setEffectiveOn] = useState(detail.as_of)
  const [reason, setReason] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit() {
    if (busy || !reason.trim()) { setError('A reason is required.'); return }
    setBusy(true); setError('')
    try {
      const result = await lifecycleCommand({
        kind: closing ? 'PERSONAL_DEBT_CLOSE' : 'PERSONAL_DEBT_REOPEN',
        debtId: debt.debt_id, generation, effectiveOn, reason: reason.trim(),
      })
      await onSaved(result.status)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not update the debt.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <details className="debt-lifecycle">
      <summary>{closing ? 'Close this debt' : 'Reopen this debt'}</summary>
      <p className="muted-note">
        {closing
          ? 'Close is allowed only when the principal is zero as of this date and no event is dated after it.'
          : 'Reopen before recording any further events; the principal is unchanged.'}
      </p>
      <div className="debt-lifecycle-row">
        <input aria-label="Lifecycle date" type="date" value={effectiveOn}
          min={debt.opened_on} onChange={(e) => setEffectiveOn(e.target.value)} disabled={busy} />
        <input aria-label="Lifecycle reason" placeholder="Reason" value={reason}
          onChange={(e) => setReason(e.target.value)} disabled={busy} />
        <Button size="sm" onClick={submit} disabled={busy}>
          {busy ? 'Saving…' : closing ? 'Close' : 'Reopen'}
        </Button>
      </div>
      {error ? <p className="debt-event-error">{error}</p> : null}
    </details>
  )
}

function DebtDetailView({
  detail, generation, categories, startRecording = false, onBack, onSaved,
}: {
  detail: DebtDetail
  generation: string
  categories: string[]
  /** Open with the event form already showing -- used right after a debt is
   * created, since a new debt carries no amount until its first event. */
  startRecording?: boolean
  onBack: () => void
  onSaved: (status: string) => void | Promise<void>
}) {
  const debt = detail.debt
  const [recording, setRecording] = useState(startRecording)
  // An event being corrected or removed; every other control waits.
  const [correcting, setCorrecting] = useState<{ event: DebtEvent; remove: boolean } | null>(null)
  const [planOpen, setPlanOpen] = useState(false)
  const busy = recording || correcting !== null || planOpen
  async function corrected(status: string) { setCorrecting(null); await onSaved(status) }
  return (
    <Card className="debt-detail"><WorkspaceReportingDetails data={detail}/>
      <Button variant="ghost" size="sm" onClick={onBack}><ArrowLeft size={14} /> All debts</Button>
      <div className="debt-detail-head">
        <div>
          <h2>{debt.counterparty_label}</h2>
          <p>
            {debt.direction === 'PAYABLE' ? 'I owe' : 'Owed to me'} · opened {dateLabel(debt.opened_on)}
            {debt.due_on ? ` · due ${dateLabel(debt.due_on)}` : ''} · {debt.status}
          </p>
        </div>
        <div className="debt-detail-balance">
          <span>Remaining principal</span>
          <strong>{reportedMoney(debt.remaining_principal, 'value', debt.currency)}</strong>
          <small>as of {detail.as_of}</small>
        </div>
      </div>
      <p className="debt-detail-status">
        {RECONCILIATION_LABEL[debt.reconciliation_status] ?? debt.reconciliation_status}
      </p>
      {debt.overlapping_commitments?.length ? (
        <p className="debt-event-error" role="note">
          {debt.overlapping_commitments.length === 1 ? 'A recorded payment of this debt is also' : 'Recorded payments of this debt are also'} part of
          {' '}{debt.overlapping_commitments.map((c) => `“${c.name}”`).join(', ')}, {debt.overlapping_commitments.length === 1 ? 'a confirmed recurring commitment' : 'confirmed recurring commitments'}.
          {' '}The forecast projects the commitment and this debt’s remaining principal separately, so the same repayments are counted twice.
        </p>
      ) : null}
      {!recording && !correcting && <RepaymentPlan detail={detail} generation={generation} onSaved={onSaved} onBusy={setPlanOpen} />}
      {correcting && (correcting.remove
        ? <RemoveEventForm detail={detail} event={correcting.event} generation={generation}
            onCancel={() => setCorrecting(null)} onSaved={corrected} />
        : <CorrectEventForm detail={detail} event={correcting.event} generation={generation} categories={categories}
            onCancel={() => setCorrecting(null)} onSaved={corrected} />)}
      {debt.status === 'OPEN' && !correcting && (
        recording ? (
          <EventForm
            detail={detail}
            generation={generation}
            categories={categories}
            onCancel={() => setRecording(false)}
            onSaved={async (status) => { setRecording(false); await onSaved(status) }}
          />
        ) : (
          <Button size="sm" variant="outline" onClick={() => setRecording(true)}>
            <Plus size={14} /> Record an event
          </Button>
        )
      )}
      {!busy && <DetailsControls detail={detail} generation={generation} onSaved={onSaved} />}
      {!busy && (
        <LifecycleControls detail={detail} generation={generation} onSaved={onSaved} />
      )}
      <table className="debt-history">
        <thead>
          <tr><th>Date</th><th>Event</th><th>Principal</th><th>Note</th><th /></tr>
        </thead>
        <tbody>
          {debt.principal_history.map((e) => (
            <tr key={e.event_id}>
              <td>{dateLabel(e.effective_on)}</td>
              <td>
                {kindLabel(e.event_kind)}
                {e.opening_balance ? <span className="tag">opening</span> : null}
                {!e.bound ? <span className="tag warn">unverified</span> : null}
              </td>
              <td>{(e.signed < 0 ? '−' : '+') + reportedMoney(e.principal, 'value', debt.currency).replace(/^−/, '')}</td>
              <td>{e.reason}{e.expense_category ? <span className="tag">{e.expense_category}</span> : null}</td>
              <td className="debt-history-actions">
                <Button size="sm" variant="ghost" disabled={busy} aria-label={`Correct ${kindLabel(e.event_kind)} on ${e.effective_on}`}
                  onClick={() => setCorrecting({ event: e, remove: false })}>Correct</Button>
                <Button size="sm" variant="ghost" disabled={busy} aria-label={`Remove ${kindLabel(e.event_kind)} on ${e.effective_on}`}
                  onClick={() => setCorrecting({ event: e, remove: true })}>Remove</Button>
              </td>
            </tr>
          ))}
          {debt.principal_history.length === 0 && (
            <tr><td colSpan={5} className="debt-empty">No principal events recorded.</td></tr>
          )}
        </tbody>
      </table>
      <p className="debt-detail-foot">
        Payables and receivables are never netted. Balances step at effective dates; day precision
        does not imply intraday order.
      </p>
    </Card>
  )
}

export function DebtsView({
  generation, categories = [], onSaved,
}: { generation: string; categories?: string[]; onSaved?: (status: string) => void | Promise<void> }) {
  const [page, setPage] = useState<DebtsPage | null>(null)
  const [detail, setDetail] = useState<DebtDetail | null>(null)
  const [creating, setCreating] = useState(false)
  const [splitting, setSplitting] = useState(false)
  const [error, setError] = useState('')
  // A debt just created, to reopen at its first event once the ledger has
  // reloaded under the generation that includes it.
  const [justCreated, setJustCreated] = useState<string | null>(null)
  const [recordOnOpen, setRecordOnOpen] = useState(false)

  useEffect(() => {
    let live = true
    setError('')
    loadDebts({ generation })
      .then((p) => { if (live) setPage(p) })
      .catch((e) => { if (live) setError(e instanceof ApiError ? e.message : 'Could not load debts.') })
    return () => { live = false }
  }, [generation])

  function openDetail(id: string, record = false) {
    setError('')
    setCreating(false)
    loadDebtDetail(id, { generation })
      .then((d) => { setRecordOnOpen(record); setDetail(d) })
      .catch((e) => setError(e instanceof ApiError ? e.message : 'Could not load that debt.'))
  }

  useEffect(() => {
    if (!justCreated) return
    setJustCreated(null)
    openDetail(justCreated, true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [justCreated, generation])

  if (error) return <Card className="debt-list"><p className="debt-empty">{error}</p></Card>
  if (creating) return (
    <NewDebtForm
      generation={generation}
      onCancel={() => setCreating(false)}
      onSaved={async (status, debtId) => {
        setCreating(false)
        await onSaved?.(status)
        setJustCreated(debtId)
      }}
    />
  )
  if (splitting && page) return (
    <SplitBillForm
      generation={generation}
      categories={categories}
      receivables={page.owed_to_me}
      onCancel={() => setSplitting(false)}
      onSaved={async (status) => {
        setSplitting(false)
        await onSaved?.(status)
      }}
    />
  )
  if (detail) return (
    <DebtDetailView
      key={detail.debt.debt_id}
      detail={detail}
      generation={generation}
      categories={categories}
      startRecording={recordOnOpen}
      onBack={() => setDetail(null)}
      onSaved={async (status) => {
        setDetail(null)
        await onSaved?.(status)
      }}
    />
  )
  if (!page) return <Card className="debt-list"><p className="debt-empty">Loading debts…</p></Card>

  return (
    <div className="debts-view"><WorkspaceReportingDetails data={page}/>
      <div className="debts-toolbar">
        <p className="debts-asof">Balances as of {page.as_of}. Payables and receivables shown separately.</p>
        <span className="debts-toolbar-actions">
          <Button size="sm" variant="outline" onClick={() => setSplitting(true)}><Split size={14} /> Split a bill</Button>
          <Button size="sm" onClick={() => setCreating(true)}><Plus size={14} /> New debt</Button>
        </span>
      </div>
      <div className="debts-columns">
        <DebtList
          title="I owe" caption="Money you still owe other people."
          debts={page.i_owe} onOpen={openDetail}
        />
        <DebtList
          title="Owed to me" caption="Money other people still owe you."
          debts={page.owed_to_me} onOpen={openDetail}
        />
      </div>
    </div>
  )
}
