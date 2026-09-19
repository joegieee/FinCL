import { useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { ApiError, money } from '@/lib/api'
import { DebtHandoff } from '@/features/classification/Handoffs'
import { ConfirmPlanForm } from '@/features/installments/ConfirmPlanForm'
import { loadCandidates, type PlanCandidate } from '@/features/installments/api'

export type SuggestedDebt = { debt_id: string; counterparty_label: string; direction: 'PAYABLE' | 'RECEIVABLE'; currency: string; earlier: number }

type RelationshipRecord = { anchor: string; description: string | null; account: string; currency: string; amount: string; date: string; direction: string }
type RelationshipCandidate = {
  candidate_id: string; kind: string; label: string; transaction_count: number
  suggested_debts?: SuggestedDebt[]; relationship_basis?: string
  installment_candidate_key?: string; installment_ambiguous?: boolean
}

/**
 * The review panel for the inbox's relationship-discovery focuses ("Looks like
 * a debt movement", "Installment candidates"). Unlike grouped categorization
 * there is no field to set for the whole group: each proposal is confirmed
 * through the owning workflow's own form, and the card's evidence is shown so
 * the user can see why the row was proposed at all.
 */
export function RelationshipReview({ candidate, records, generation, disabled, onRecorded, onRefresh, onClose }: {
  candidate: RelationshipCandidate
  records: RelationshipRecord[]
  generation: string
  disabled: boolean
  /** A relationship was recorded: refresh the queue and move on. */
  onRecorded: (status: string) => Promise<void>
  /** The ledger changed without resolving this group (e.g. a new debt). */
  onRefresh: (status: string) => Promise<void>
  onClose: () => void
}) {
  const isInstallment = candidate.kind === 'installment_chain' || candidate.kind === 'installment_ambiguous'
  return <Card className="categorization-panel candidate-review" role="region" aria-label={`Review ${candidate.label}`}>
    <div className="candidate-review-header"><div className="panel-title"><div>
      <p className="eyebrow">{isInstallment ? 'POSSIBLE INSTALLMENT PLAN' : 'POSSIBLE DEBT MOVEMENTS'}</p>
      <h2>{candidate.label}</h2>
      {candidate.relationship_basis && <p className="muted-note">{candidate.relationship_basis}</p>}
    </div>
    <span className="candidate-review-nav"><Button variant="ghost" size="sm" onClick={onClose}>Close</Button></span>
    </div></div>
    {disabled && <p className="form-error">Refresh the ledger before recording anything.</p>}
    {isInstallment
      ? <InstallmentProposal candidate={candidate} records={records} generation={generation} disabled={disabled} onRecorded={onRecorded}/>
      : <DebtProposals candidate={candidate} records={records} generation={generation} disabled={disabled} onRecorded={onRecorded} onRefresh={onRefresh}/>}
  </Card>
}

function recordLine(row: RelationshipRecord) {
  return <><span>{row.date} · {row.account}</span><strong>{money(row.amount, row.currency)}</strong><small>{row.description || 'No description'}</small></>
}

function DebtProposals({ candidate, records, generation, disabled, onRecorded, onRefresh }: {
  candidate: RelationshipCandidate; records: RelationshipRecord[]; generation: string; disabled: boolean
  onRecorded: (status: string) => Promise<void>; onRefresh: (status: string) => Promise<void>
}) {
  // One debt event per movement: each repayment has its own date, so they are
  // recorded one at a time rather than folded into a single event.
  const [open, setOpen] = useState<string | null>(null)
  const preferred = candidate.suggested_debts?.[0]
  return <div className="candidate-records">
    <p className="muted-note">
      Record each one against the debt it belongs to, or leave it: nothing changes until you save.
      {preferred ? ` ${preferred.counterparty_label} is pre-selected because it has the most earlier movements with this payee.` : ''}
    </p>
    {records.map(row => <div key={row.anchor} className="relationship-record">
      <div className="candidate-record-row">{recordLine(row)}</div>
      {open === row.anchor
        ? <DebtHandoff entry={{ anchor: row.anchor, currency: row.currency, date: row.date, direction: row.direction === 'IN' ? 'IN' : 'OUT' }}
            generation={generation} preferredDebtId={preferred?.debt_id}
            onRecorded={async status => { setOpen(null); await onRecorded(status) }} onRefresh={onRefresh}/>
        : <Button type="button" variant="outline" size="sm" disabled={disabled} onClick={() => setOpen(row.anchor)}>Record against a debt</Button>}
    </div>)}
  </div>
}

function InstallmentProposal({ candidate, records, generation, disabled, onRecorded }: {
  candidate: RelationshipCandidate; records: RelationshipRecord[]; generation: string; disabled: boolean
  onRecorded: (status: string) => Promise<void>
}) {
  const [proposal, setProposal] = useState<PlanCandidate | null>(null)
  const [confirming, setConfirming] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    if (!candidate.installment_candidate_key) return
    let live = true
    loadCandidates({ generation })
      .then(page => {
        if (!live) return
        const found = page.candidates.find(c => c.candidate_key === candidate.installment_candidate_key)
        if (found) setProposal(found)
        else setError('This proposal is no longer current; refresh the inbox.')
      })
      .catch(e => { if (live) setError(e instanceof ApiError ? e.message : 'Could not load the proposal.') })
    return () => { live = false }
  }, [candidate.installment_candidate_key, generation])
  return <div className="candidate-records">
    {records.map(row => <div key={row.anchor} className="candidate-record-row">{recordLine(row)}</div>)}
    {candidate.installment_ambiguous
      ? <p className="muted-note">Confirm one plan from the Installments page to separate these chains.</p>
      : <>
        {error && <p className="form-error" role="alert">{error}</p>}
        {proposal && (confirming
          ? <ConfirmPlanForm candidate={proposal} generation={generation} onSaved={onRecorded} onCancel={() => setConfirming(false)}/>
          : <div className="dialog-actions"><Button type="button" disabled={disabled} onClick={() => setConfirming(true)}>Confirm this plan</Button></div>)}
      </>}
  </div>
}
