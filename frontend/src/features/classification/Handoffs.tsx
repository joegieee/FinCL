import { useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { ApiError, money, type Dashboard, type Entry, type Proposal } from '@/lib/api'
import { dateLabel } from '@/lib/format'
import { useLedgerCommand } from '@/lib/command'
import { EventForm } from '@/features/debts/EventForm'
import { NewDebtForm } from '@/features/debts/NewDebtForm'
import {
  kindsForMovement, loadAllocationCandidates, loadDebtDetail, loadDebts,
  type AllocationCandidate, type DebtCard, type DebtDetail,
} from '@/features/debts/api'
import { PurchaseAttributionForm } from '@/features/installments/PurchaseAttributionForm'
import {
  loadEntryCandidates, loadInstallmentDetail, loadInstallments,
  type EntryCandidate, type InstallmentDetail, type PlanShape,
} from '@/features/installments/api'

/**
 * The relationship hand-offs behind the single-transaction editor's "What is
 * this?" choice. Each one opens an existing relationship workflow's own form
 * (debt event, installment attribution, transfer decision) already pointed at
 * this transaction, and reports back once it has recorded a decision, so the
 * editor can re-read what that decision made of the row. None of them writes
 * a classification of its own.
 */

/** The bank direction of a list-shaped entry (its amount is signed). */
export function movementDirection(entry: Entry): 'IN' | 'OUT' {
  return entry.amount.startsWith('-') ? 'OUT' : 'IN'
}

/** The little a hand-off needs to know about the transaction it points a
 * relationship form at -- enough to come from the transaction list or from a
 * categorization inbox record alike. */
export type HandoffTarget = { anchor: string; currency: string; date: string; direction: 'IN' | 'OUT' }

export function handoffTarget(entry: Entry): HandoffTarget {
  return { anchor: entry.anchor, currency: entry.currency, date: entry.date, direction: movementDirection(entry) }
}

/** A generation race while the parent catches up with a write we just made is
 * not worth showing: the effect reruns as soon as the fresh token arrives. */
function isStale(e: unknown): boolean {
  return (e as Error).message.startsWith('STALE_GENERATION')
}

function message(e: unknown, fallback: string): string {
  return e instanceof ApiError || e instanceof Error ? e.message : fallback
}

// ---------------------------------------------------------------------------
// Debt
// ---------------------------------------------------------------------------

export function DebtHandoff({ entry, generation, onRecorded, onRefresh, preferredDebtId }: {
  entry: HandoffTarget
  generation: string
  /** Pre-selects a debt the caller has evidence for (the inbox's "Looks like
   * a debt movement" focus); still only honoured if this movement can back it. */
  preferredDebtId?: string
  /** A debt event was recorded against this transaction. */
  onRecorded: (status: string) => Promise<void>
  /** The ledger changed without touching this transaction (a new debt). */
  onRefresh: (status: string) => Promise<void>
}) {
  const direction = entry.direction
  const [debts, setDebts] = useState<DebtCard[] | null>(null)
  const [picked, setPicked] = useState<string | null>(preferredDebtId ?? null)
  const [detail, setDetail] = useState<DebtDetail | null>(null)
  const [seed, setSeed] = useState<AllocationCandidate | null>(null)
  const [unusable, setUnusable] = useState(false)
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    let live = true
    loadDebts({ generation })
      .then(page => {
        if (!live) return
        // Only debts this one movement could back: open, same currency, and
        // with an event kind whose bank direction matches it.
        setDebts([...page.i_owe, ...page.owed_to_me].filter(debt =>
          debt.status === 'OPEN' && debt.currency === entry.currency && kindsForMovement(debt.direction, direction).length > 0))
      })
      .catch(e => { if (live && !isStale(e)) setError(message(e, 'Could not load debts.')) })
    return () => { live = false }
  }, [generation, entry.currency, direction])

  useEffect(() => {
    if (!picked || !debts) return
    const debt = debts.find(d => d.debt_id === picked)
    if (!debt) return
    let live = true
    setDetail(null); setSeed(null); setUnusable(false); setError('')
    const kind = kindsForMovement(debt.direction, direction)[0]
    Promise.all([
      loadDebtDetail(picked, { generation }),
      loadAllocationCandidates({ debtId: picked, eventKind: kind.kind, generation, anchor: entry.anchor }),
    ])
      .then(([loaded, candidates]) => {
        if (!live) return
        setDetail(loaded)
        const own = candidates.candidates.find(c => c.entry_anchor === entry.anchor)
        if (own) setSeed(own)
        else setUnusable(true)
      })
      .catch(e => { if (live && !isStale(e)) setError(message(e, 'Could not load that debt.')) })
    return () => { live = false }
  }, [picked, debts, generation, entry.anchor, direction])

  const outgoing = direction === 'OUT'
  if (creating) {
    return <NewDebtForm generation={generation}
      initial={{ currency: entry.currency, openedOn: entry.date, direction: outgoing ? 'RECEIVABLE' : 'PAYABLE' }}
      onCancel={() => setCreating(false)}
      onSaved={async (status, debtId) => { setCreating(false); await onRefresh(status); setPicked(debtId) }}/>
  }
  return <div className="handoff">
    <p className="muted-note">
      {outgoing
        ? 'Money you lent, repaid, or paid on someone else’s behalf is debt principal, not spending.'
        : 'Money you borrowed, or that someone paid back to you, is debt principal, not income.'}
      {' '}Record it against the debt and it leaves your spending and income totals.
    </p>
    {error && <p className="form-error" role="alert">{error}</p>}
    {debts === null && !error && <p className="muted-note">Loading debts…</p>}
    {debts && <label>Debt
      <select aria-label="Debt" value={picked ?? ''} onChange={e => setPicked(e.target.value || null)}>
        <option value="">{debts.length ? 'Choose a debt' : `No open ${entry.currency} debt this could belong to`}</option>
        {debts.map(debt => <option key={debt.debt_id} value={debt.debt_id}>
          {debt.counterparty_label} · {debt.direction === 'PAYABLE' ? 'I owe' : 'owed to me'} · {money(debt.remaining_principal.value, debt.currency)} remaining
        </option>)}
      </select>
    </label>}
    {!picked && <Button type="button" variant="outline" size="sm" onClick={() => setCreating(true)}>Start a new debt</Button>}
    {unusable && <p className="form-error">This transaction can't be recorded against that debt: it is already fully allocated elsewhere.</p>}
    {detail && seed && <EventForm key={`${picked}-${seed.entry_anchor}-${seed.open_amount}`} detail={detail} generation={generation} seed={seed}
      onSaved={onRecorded} onCancel={() => setPicked(null)}/>}
  </div>
}

// ---------------------------------------------------------------------------
// Installment
// ---------------------------------------------------------------------------

export function InstallmentHandoff({ entry, generation, onRecorded, onCancel }: {
  entry: HandoffTarget
  generation: string
  onRecorded: (status: string) => Promise<void>
  onCancel: () => void
}) {
  const [plans, setPlans] = useState<PlanShape[] | null>(null)
  const [picked, setPicked] = useState<string | null>(null)
  const [detail, setDetail] = useState<InstallmentDetail | null>(null)
  const [seed, setSeed] = useState<EntryCandidate | null>(null)
  const [unusable, setUnusable] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    let live = true
    loadInstallments({ generation })
      .then(page => { if (live) setPlans(page.plans.filter(plan => plan.bound && plan.currency === entry.currency)) })
      .catch(e => { if (live && !isStale(e)) setError(message(e, 'Could not load installment plans.')) })
    return () => { live = false }
  }, [generation, entry.currency])

  useEffect(() => {
    if (!picked) return
    let live = true
    setDetail(null); setSeed(null); setUnusable(false); setError('')
    Promise.all([
      loadInstallmentDetail(picked, { generation }),
      loadEntryCandidates({ planId: picked, purpose: 'purchase', generation, anchor: entry.anchor }),
    ])
      .then(([loaded, candidates]) => {
        if (!live) return
        setDetail(loaded)
        const own = candidates.candidates.find(c => c.entry_anchor === entry.anchor)
        if (own) setSeed(own)
        else setUnusable(true)
      })
      .catch(e => { if (live && !isStale(e)) setError(message(e, 'Could not load that plan.')) })
    return () => { live = false }
  }, [picked, generation, entry.anchor])

  return <div className="handoff">
    <p className="muted-note">
      An installment plan splits a purchase into payments; the purchase is still spending, so keep its category.
      Linking it tells the plan which charge it splits.
    </p>
    {error && <p className="form-error" role="alert">{error}</p>}
    {plans === null && !error && <p className="muted-note">Loading plans…</p>}
    {plans && <label>Installment plan
      <select aria-label="Installment plan" value={picked ?? ''} onChange={e => setPicked(e.target.value || null)}>
        <option value="">{plans.length ? 'Choose a plan' : `No ${entry.currency} installment plan yet — confirm one on the Installments page`}</option>
        {plans.map(plan => <option key={plan.plan_id} value={plan.plan_id}>{plan.merchant_label} · opened {plan.opened_on}</option>)}
      </select>
    </label>}
    {unusable && <p className="form-error">This plan can't use this transaction as its purchase: it is on a different card, already claimed, or itself an instalment payment.</p>}
    {detail && seed && <PurchaseAttributionForm key={`${picked}-${seed.entry_anchor}`} detail={detail} generation={generation} seed={seed}
      onSaved={onRecorded} onCancel={onCancel}/>}
    {!detail && <Button type="button" variant="ghost" size="sm" onClick={onCancel}>Cancel</Button>}
  </div>
}

// ---------------------------------------------------------------------------
// Transfer
// ---------------------------------------------------------------------------

export function TransferHandoff({ entry, data, onRecorded, onFromOwnAccount }: {
  entry: Entry
  data: Dashboard
  onRecorded: (status: string) => Promise<void>
  /** Only offered for money in: mark it as a self-transfer from an account
   * that is not imported, through the ordinary incoming-type field. */
  onFromOwnAccount?: () => void
}) {
  const proposals = data.transfer_proposals.filter(p => p.left === entry.anchor || p.right === entry.anchor)
  return <div className="handoff">
    <p className="muted-note">A transfer between your own accounts is neither spending nor income.</p>
    {proposals.map(p => <ProposalDecision key={p.id} proposal={p} anchor={entry.anchor} data={data} onRecorded={onRecorded}/>)}
    {!proposals.length && <>
      <p className="muted-note">
        No matching transaction on another of your accounts was found for this one.
        {' '}If the other side is in an account you have not imported, importing it and refreshing will propose the match.
      </p>
      {onFromOwnAccount
        ? <Button type="button" variant="outline" size="sm" onClick={onFromOwnAccount}>It came from an account I don't import</Button>
        : <p className="muted-note">Linking two transactions by hand is not available here yet.</p>}
    </>}
  </div>
}

function ProposalDecision({ proposal: p, anchor, data, onRecorded }: {
  proposal: Proposal; anchor: string; data: Dashboard; onRecorded: (status: string) => Promise<void>
}) {
  const [decision, setDecision] = useState<'confirm' | 'reject' | null>(null)
  const [reason, setReason] = useState('')
  const cmd = useLedgerCommand(data.generation, onRecorded)
  const mineIsLeft = p.left === anchor
  const other = mineIsLeft
    ? { account: p.right_account, date: p.right_date, description: p.right_description, amount: p.right_amount, currency: p.right_currency }
    : { account: p.left_account, date: p.left_date, description: p.left_description, amount: p.left_amount, currency: p.left_currency }
  const disabled = data.freshness === 'stale' || cmd.busy
  return <div className="proposal">
    <p><strong>Possible other side:</strong> {other.account} · {dateLabel(other.date)}{other.description ? ` · ${other.description}` : ''} · {money(other.amount, other.currency)}</p>
    <p className="proposal-basis">{p.link_kind === 'CURRENCY_EXCHANGE' ? 'Currency exchange' : 'Same-currency transfer'} · {p.basis}</p>
    {!decision
      ? <div className="dialog-actions">
          <Button type="button" size="sm" disabled={disabled} onClick={() => { setDecision('confirm'); cmd.reset() }}>Confirm transfer</Button>
          <Button type="button" size="sm" variant="outline" disabled={disabled} onClick={() => { setDecision('reject'); cmd.reset() }}>Not a transfer</Button>
        </div>
      : <div>
          <label>Reason
            <Input maxLength={2000} disabled={cmd.attempted} value={reason}
              placeholder={decision === 'confirm' ? 'Why are these the same transfer?' : 'Why is this not an internal transfer?'}
              onChange={e => { setReason(e.target.value); cmd.reset() }}/>
          </label>
          {cmd.error && <p className="form-error" role="alert">{cmd.error}</p>}
          <p className="muted-note">{decision === 'confirm' ? 'Both legs become an internal transfer and leave spending and income.' : 'The pair stays as two ordinary transactions.'}</p>
          <div className="dialog-actions">
            <Button type="button" size="sm" variant="ghost" disabled={cmd.busy} onClick={() => { setDecision(null); cmd.reset() }}>Back</Button>
            <Button type="button" size="sm" disabled={disabled || !reason.trim()}
              onClick={() => void cmd.act({ kind: 'TRANSFER_DECISION', anchors: { left: p.left, right: p.right }, payload: { decision, reason: reason.trim() } })}>
              {cmd.busy ? 'Working…' : cmd.previewed ? 'Save decision' : 'Check decision'}
            </Button>
          </div>
        </div>}
  </div>
}
