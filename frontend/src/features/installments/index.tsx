import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, ArrowLeft, ChevronRight, Plus } from 'lucide-react'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { AsyncView } from '@/components/AsyncView'
import { ApiError, money } from '@/lib/api'
import { ConfirmPlanForm } from './ConfirmPlanForm'
import { PurchaseAttributionForm } from './PurchaseAttributionForm'
import { SettlementForm } from './SettlementForm'
import {
  loadCandidates,
  loadInstallmentDetail,
  loadInstallments,
  retirePlan,
  type CandidatesPage,
  type InstallmentDetail,
  type InstallmentsPage,
  type PlanCandidate,
  type PlanShape,
} from './api'

function PlanRow({ plan, onOpen }: { plan: PlanShape; onOpen: (id: string) => void }) {
  const p = plan.progress
  return (
    <button className="debt-row" onClick={() => onOpen(plan.plan_id)}>
      <span className="debt-row-main">
        <strong>{plan.merchant_label}</strong>
        <small>
          {plan.effective_status} · {plan.interpretation_status}
          {' · '}
          {p.known_principal ? `principal ${money(p.known_principal.value, plan.currency)}` : 'principal unknown'}
          {' · '}
          {p.occurrences_bound}/{p.occurrence_count} occurrences
        </small>
      </span>
      <span className="debt-row-amount">
        {p.remaining_obligation
          ? money(p.remaining_obligation.value, plan.currency)
          : 'Not determinable'}
      </span>
      <ChevronRight size={15} />
    </button>
  )
}

function CandidateRow({
  candidate,
  onConfirm,
}: {
  candidate: PlanCandidate
  onConfirm: (candidate: PlanCandidate) => void
}) {
  return (
    <div className="debt-row candidate-row">
      <span className="debt-row-main">
        <strong>{candidate.merchant_label}</strong>
        <small>
          {candidate.family} · {candidate.observed_count} of {candidate.term_months} instalments
          {' · '}
          from {candidate.opened_on}
          {' · '}
          {candidate.complete ? 'fully paid' : `${candidate.term_months - candidate.observed_count} still to come`}
        </small>
      </span>
      <span className="debt-row-amount">
        {money(candidate.observed_total.value, candidate.currency)}
      </span>
      <Button size="sm" onClick={() => onConfirm(candidate)}>
        <Plus size={13} /> Confirm
      </Button>
    </div>
  )
}

function DetailView({
  detail,
  generation,
  onBack,
  onSaved,
}: {
  detail: InstallmentDetail
  generation: string
  onBack: () => void
  onSaved: (status: string) => void | Promise<void>
}) {
  const { plan, views } = detail
  const currency = plan.currency
  const [mode, setMode] = useState<'view' | 'settle' | 'attribute'>('view')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function retire() {
    const reason = window.prompt(
      'Retiring removes this plan and its allocations and settlement. Why?',
    )
    if (!reason?.trim()) return
    setBusy(true)
    setError('')
    try {
      const result = await retirePlan({ planId: plan.plan_id, generation, reason: reason.trim() })
      await onSaved(result.status)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not retire this plan.')
    } finally {
      setBusy(false)
    }
  }

  if (mode === 'settle') {
    return (
      <Card className="debt-detail">
        <SettlementForm
          detail={detail}
          generation={generation}
          onSaved={onSaved}
          onCancel={() => setMode('view')}
        />
      </Card>
    )
  }

  if (mode === 'attribute') {
    return (
      <Card className="debt-detail">
        <PurchaseAttributionForm
          detail={detail}
          generation={generation}
          onSaved={onSaved}
          onCancel={() => setMode('view')}
        />
      </Card>
    )
  }

  return (
    <Card className="debt-detail">
      <Button variant="ghost" size="sm" onClick={onBack}><ArrowLeft size={14} /> All plans</Button>
      <div className="debt-detail-head">
        <div>
          <h2>{plan.merchant_label}</h2>
          <p>
            {plan.plan_reference ? `ref ${plan.plan_reference} · ` : ''}
            opened {plan.opened_on} · {plan.effective_status} · {plan.interpretation_status}
          </p>
        </div>
        <div className="debt-detail-balance">
          <span>Remaining obligation</span>
          <strong>
            {plan.progress.remaining_obligation
              ? money(plan.progress.remaining_obligation.value, currency)
              : 'Not determinable'}
          </strong>
          <small>
            {plan.progress.known_principal
              ? `of ${money(plan.progress.known_principal.value, currency)} recognized`
              : 'principal is unknown — nothing inferred'}
          </small>
        </div>
      </div>

      <div className="debt-event-actions">
        <Button size="sm" variant="outline" onClick={() => setMode('attribute')} disabled={busy}>
          {detail.purchase_allocations.length ? 'Reattribute purchase' : 'Attribute purchase'}
        </Button>
        <Button size="sm" variant="outline" onClick={() => setMode('settle')} disabled={busy}>
          Settle early
        </Button>
        <Button size="sm" variant="ghost" onClick={retire} disabled={busy}>
          {busy ? 'Working…' : 'Retire plan'}
        </Button>
      </div>
      {error ? <p className="debt-event-error">{error}</p> : null}

      <h3>Source occurrences</h3>
      <p className="muted-note">Occurrence identity is an evidence anchor, not list position.</p>
      <table className="debt-history">
        <thead><tr><th>n / of</th><th>Effective</th><th>Amount</th><th>Bound</th></tr></thead>
        <tbody>
          {detail.source_occurrences.map((o) => (
            <tr key={o.occurrence_id}>
              <td>{(o.occurrence_index ?? '?') + ' / ' + (o.occurrence_count ?? '?')}</td>
              <td>{o.effective_on}{o.date_precision !== 'DAY' ? ` (${o.date_precision})` : ''}</td>
              <td>{money(o.amount.value, currency)}</td>
              <td>{o.bound ? 'yes' : 'no'}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {detail.schedule.length > 0 && (
        <>
          <h3>Projected schedule</h3>
          <p className="muted-note">Distinct from the observed occurrences, even after a due date passes.</p>
          <table className="debt-history">
            <thead><tr><th>#</th><th>Due</th><th>Principal</th><th>Fee</th><th>Rounding remainder</th></tr></thead>
            <tbody>
              {detail.schedule.map((s) => (
                <tr key={s.schedule_index}>
                  <td>{s.schedule_index}</td>
                  <td>{s.due_on}</td>
                  <td>{money(s.principal.value, currency)}</td>
                  <td>{money(s.fee.value, currency)}</td>
                  <td>{money(s.rounding_remainder.value, currency)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {detail.purchase_allocations.length === 0 && (
        <>
          <h3>Original purchase attribution</h3>
          <p className="muted-note">
            No purchase is linked to this plan yet, so its recognized cost is unknown.
            Use “Attribute purchase” to point at the charge it splits.
          </p>
        </>
      )}

      {detail.purchase_allocations.length > 0 && (
        <>
          <h3>Original purchase attribution</h3>
          <table className="debt-history">
            <thead><tr><th>Source</th><th>Component</th><th>Amount</th><th>Bound</th></tr></thead>
            <tbody>
              {detail.purchase_allocations.map((a, i) => (
                <tr key={i}>
                  <td>{a.entry_anchor.slice(0, 16)}…</td>
                  <td>{a.component_role}</td>
                  <td>{money(a.amount.value, currency)}</td>
                  <td>{a.bound ? 'yes' : 'no'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {detail.settlements.length > 0 && (
        <>
          <h3>Early settlement</h3>
          <table className="debt-history">
            <thead><tr><th>Date</th><th>Principal</th><th>Fees</th><th>Waived</th></tr></thead>
            <tbody>
              {detail.settlements.map((s) => (
                <tr key={s.settlement_id}>
                  <td>{s.settled_on}</td>
                  <td>{money(s.principal.value, currency)}</td>
                  <td>{money(s.fee.value, currency)}</td>
                  <td>{money(s.waiver.value, currency)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      <div className="card-lenses">
        <div>
          <h4>Recognized purchase cost</h4>
          <p>
            {views.recognized_purchase_cost.recognized_principal
              ? money(views.recognized_purchase_cost.recognized_principal.value, currency)
              : 'Unknown'}
            {views.recognized_purchase_cost.financing_fee
              ? ` + ${money(views.recognized_purchase_cost.financing_fee.value, currency)} financing fee`
              : ''}
          </p>
          <small>{views.recognized_purchase_cost.note}</small>
        </div>
        <div>
          <h4>Spread allocation</h4>
          <p>{views.spread_allocation.observed_occurrences.length} observed occurrence(s)</p>
          <small>{views.spread_allocation.note}</small>
        </div>
      </div>
    </Card>
  )
}

export function InstallmentsView({
  generation,
  onSaved,
}: {
  generation: string
  onSaved?: (status: string) => void | Promise<void>
}) {
  const [page, setPage] = useState<InstallmentsPage | null>(null)
  const [candidates, setCandidates] = useState<CandidatesPage | null>(null)
  const [detail, setDetail] = useState<InstallmentDetail | null>(null)
  const [confirming, setConfirming] = useState<PlanCandidate | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const [plans, proposed] = await Promise.all([
        loadInstallments({ generation }),
        loadCandidates({ generation }),
      ])
      setPage(plans)
      setCandidates(proposed)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not load installments.')
    } finally {
      setLoading(false)
    }
  }, [generation])

  useEffect(() => { void load() }, [load])

  function openDetail(id: string) {
    setError('')
    loadInstallmentDetail(id, { generation })
      .then(setDetail)
      .catch((e) => setError(e instanceof ApiError ? e.message : 'Could not load that plan.'))
  }

  // A write advances the generation, so the parent reloads the dashboard and
  // this view re-runs against the new token rather than showing stale rows.
  async function saved(status: string) {
    setConfirming(null)
    setDetail(null)
    await onSaved?.(status)
    await load()
  }

  if (confirming) {
    return (
      <Card className="debt-detail">
        <ConfirmPlanForm
          candidate={confirming}
          generation={generation}
          onSaved={saved}
          onCancel={() => setConfirming(null)}
        />
      </Card>
    )
  }

  if (detail) {
    return (
      <DetailView
        detail={detail}
        generation={generation}
        onBack={() => setDetail(null)}
        onSaved={saved}
      />
    )
  }

  return (
    <AsyncView
      loading={loading}
      error={error}
      data={page}
      onRetry={() => { void load() }}
      errorTitle="We couldn’t load your installment plans."
      loadingLabel="Loading installments…"
    >
      {(plans) => (
        <div className="debts-view">
          {candidates && candidates.candidates.length > 0 && (
            <Card className="debt-list">
              <header>
                <h3>Split payments to confirm</h3>
                <p>
                  Instalment chains your card statements already print. Confirming one records it
                  as a plan; nothing is counted twice, because each statement line is claimed once.
                </p>
              </header>
              {candidates.candidates.map((c) => (
                <CandidateRow key={c.candidate_key} candidate={c} onConfirm={setConfirming} />
              ))}
            </Card>
          )}

          {candidates?.ambiguous_groups.map((g, i) => (
            <Card className="debt-list" key={i}>
              <header>
                <h3><AlertTriangle size={15} /> {g.family} · {g.term_months} months</h3>
                <p>{g.reason}</p>
              </header>
              <table className="debt-history">
                <thead><tr><th>n / of</th><th>Date</th><th>Amount</th><th>Statement line</th></tr></thead>
                <tbody>
                  {g.occurrences.map((o) => (
                    <tr key={o.entry_anchor}>
                      <td>{o.occurrence_index} / {o.occurrence_count}</td>
                      <td>{o.effective_on}</td>
                      <td>{money(o.amount, g.currency)}</td>
                      <td>{o.description ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
          ))}

          <Card className="debt-list">
            <header>
              <h3>Installment plans</h3>
              <p>Confirmed source-proven chains. Similar monthly amounts alone are not a plan.</p>
            </header>
            {plans.plans.length === 0 ? (
              <p className="debt-empty">
                No installment plans confirmed yet.
                {candidates && candidates.candidates.length > 0
                  ? ' Confirm one of the split payments above to start.'
                  : ''}
              </p>
            ) : (
              plans.plans.map((p) => <PlanRow key={p.plan_id} plan={p} onOpen={openDetail} />)
            )}
          </Card>
        </div>
      )}
    </AsyncView>
  )
}
