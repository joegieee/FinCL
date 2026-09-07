import { useEffect, useState } from 'react'
import { ArrowLeft, ChevronRight } from 'lucide-react'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { ApiError, money } from '@/lib/api'
import {
  loadInstallmentDetail,
  loadInstallments,
  type InstallmentDetail,
  type InstallmentsPage,
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

function DetailView({ detail, onBack }: { detail: InstallmentDetail; onBack: () => void }) {
  const { plan, views } = detail
  const currency = plan.currency
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

export function InstallmentsView({ generation }: { generation: string }) {
  const [page, setPage] = useState<InstallmentsPage | null>(null)
  const [detail, setDetail] = useState<InstallmentDetail | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let live = true
    setError('')
    loadInstallments({ generation })
      .then((p) => { if (live) setPage(p) })
      .catch((e) => { if (live) setError(e instanceof ApiError ? e.message : 'Could not load installments.') })
    return () => { live = false }
  }, [generation])

  function openDetail(id: string) {
    setError('')
    loadInstallmentDetail(id, { generation })
      .then(setDetail)
      .catch((e) => setError(e instanceof ApiError ? e.message : 'Could not load that plan.'))
  }

  if (error) return <Card className="debt-list"><p className="debt-empty">{error}</p></Card>
  if (detail) return <DetailView detail={detail} onBack={() => setDetail(null)} />
  if (!page) return <Card className="debt-list"><p className="debt-empty">Loading installments…</p></Card>

  return (
    <div className="debts-view">
      <Card className="debt-list">
        <header>
          <h3>Installment plans</h3>
          <p>Confirmed source-proven chains. Similar monthly amounts alone are not a plan.</p>
        </header>
        {page.plans.length === 0 ? (
          <p className="debt-empty">No installment plans confirmed yet.</p>
        ) : (
          page.plans.map((p) => <PlanRow key={p.plan_id} plan={p} onOpen={openDetail} />)
        )}
      </Card>
    </div>
  )
}
