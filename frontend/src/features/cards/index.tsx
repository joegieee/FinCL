import { WorkspaceReportingDetails, reportedMoney } from '@/lib/workspaceReporting'
import { useEffect, useState } from 'react'
import { ArrowLeft, ChevronRight } from 'lucide-react'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { ApiError, money } from '@/lib/api'
import { dateLabel } from '@/lib/format'
import { SourceComparison } from './SourceComparison'
import { FacilityForm, LimitForm, RepaymentPlanForm, RetireForm } from './FacilityForms'
import {
  type CardAccount,
  RECONCILIATION_LABEL,
  loadCardDetail,
  loadCards,
  type CardDetail,
  type CardListRow,
  type CardsPage,
} from './api'

function CardRow({ row, onOpen }: { row: CardListRow; onOpen: (id: string) => void }) {
  const latest = row.latest_statement
  return (
    <button className="debt-row" onClick={() => onOpen(row.account.account_id)}>
      <span className="debt-row-main">
        <strong>{row.account.display_name}</strong>
        <small>
          {latest
            ? `Statement ${dateLabel(latest.statement_date)} · ${
                RECONCILIATION_LABEL[latest.reconciliation_status] ?? latest.reconciliation_status
              }`
            : 'No selected statement yet'}
          {row.coverage_gaps.length > 0 ? ` · ${row.coverage_gaps.length} missing period(s)` : ''}
        </small>
      </span>
      <span className="debt-row-amount">
        {latest?.closing_liability
          ? reportedMoney(latest.closing_liability, 'value', latest.closing_liability.currency ?? 'SGD')
          : 'Unavailable'}
      </span>
      <ChevronRight size={15} />
    </button>
  )
}

function Utilization({ detail }: { detail: CardDetail }) {
  const u = detail.utilization
  if (!u.available) {
    return <p className="muted-note">Utilization unavailable — {u.reason}.</p>
  }
  if (u.credit_balance) {
    return (
      <p className="muted-note">
        Credit balance of {reportedMoney(u, 'credit_balance_amount', u.currency)} — nothing is owed.
      </p>
    )
  }
  const width = Math.min(100, Math.max(0, Number(u.ratio) * 100))
  return (
    <div className="card-util">
      <div className="card-util-bar"><span style={{ width: `${width}%` }} /></div>
      <small>
        {u.basis} · ratio {u.ratio}
        {u.over_limit ? ` · over limit by ${reportedMoney(u, 'over_limit_amount', u.currency)}` : ` · ${reportedMoney(u, 'headroom', u.currency)} headroom`}
      </small>
    </div>
  )
}

type Editing = 'facility' | 'limit' | 'retire-limit' | 'retire-facility' | null

/** The facility and its dated limit, and the forms that record them. */
function LimitAndFacility({ detail, cards, generation, onSaved }: {
  detail: CardDetail; cards: CardAccount[]; generation: string; onSaved: (status: string) => void | Promise<void>
}) {
  const [editing, setEditing] = useState<Editing>(null)
  const saved = async (status: string) => { setEditing(null); await onSaved(status) }
  const close = () => setEditing(null)
  const facility = detail.facility
  const observation = detail.limit_observation
  return (
    <section className="card-limit-section">
      <h3>Credit limit</h3>
      <p className="muted-note">
        {!facility && 'No credit facility is set up for this card, so its limit is read from the latest statement only.'}
        {facility && !observation && `Facility “${facility.label}” (${facility.currency}) has no recorded limit yet.`}
        {facility && observation && `Facility “${facility.label}”: ${money(observation.limit.value, observation.limit.currency ?? facility.currency)}, seen ${dateLabel(observation.observed_on)}${observation.source_ref ? ` (${observation.source_ref})` : ''}.`}
      </p>
      {!editing && (
        <div className="row-actions">
          <Button variant="outline" size="sm" onClick={() => setEditing('facility')}>{facility ? 'Edit facility' : 'Set up facility'}</Button>
          {facility && <Button variant="outline" size="sm" onClick={() => setEditing('limit')}>Record limit</Button>}
          {observation && <Button variant="ghost" size="sm" onClick={() => setEditing('retire-limit')}>Remove limit</Button>}
          {facility && <Button variant="ghost" size="sm" onClick={() => setEditing('retire-facility')}>Remove facility</Button>}
        </div>
      )}
      {editing === 'facility' && <FacilityForm detail={detail} cards={cards} generation={generation} onSaved={saved} onCancel={close}/>}
      {editing === 'limit' && facility && <LimitForm detail={detail} generation={generation} onSaved={saved} onCancel={close}/>}
      {editing === 'retire-limit' && facility && <RetireForm detail={detail} target="limit" generation={generation} onSaved={saved} onCancel={close}/>}
      {editing === 'retire-facility' && facility && <RetireForm detail={detail} target="facility" generation={generation} onSaved={saved} onCancel={close}/>}
    </section>
  )
}

/** Was each statement paid, and how do we know. */
function Repayments({ detail }: { detail: CardDetail }) {
  const rows = detail.repayments ?? []
  if (!rows.length) return null
  const settled = rows.filter(row => row.paid_in_full).length
  return (
    <section className="card-limit-section">
      <h3>Repayments</h3>
      <p className="muted-note">
        {settled} of {rows.length} statement{rows.length === 1 ? '' : 's'} paid in full. A payment counts toward a statement when the card shows money
        arriving after that statement closed and by its due date; a payment you matched by hand takes its place.
      </p>
      <div className="table-scroll"><table className="card-repayments">
        <thead><tr><th scope="col">Statement</th><th scope="col" className="num">Due</th><th scope="col" className="num">Paid</th><th scope="col">Status</th></tr></thead>
        <tbody>{rows.map(row => (
          <tr key={`${row.statement_id}:${row.currency}`}>
            <td>{dateLabel(row.statement_date)}{row.payment_due_date && <small>due by {dateLabel(row.payment_due_date)}</small>}</td>
            <td className="num">{money(row.due, row.currency)}</td>
            <td className="num">{money(row.paid, row.currency)}</td>
            <td>{row.paid_in_full ? 'Paid in full' : row.paid_basis === 'none' ? 'No payment seen yet' : 'Part paid'}
              {row.paid_basis === 'manual_allocation' && <small>matched by hand</small>}</td>
          </tr>
        ))}</tbody>
      </table></div>
    </section>
  )
}

function RepaymentPlan({ detail, generation, onSaved }: {
  detail: CardDetail; generation: string; onSaved: (status: string) => void | Promise<void>
}) {
  const [editing, setEditing] = useState(false)
  const plan = detail.repayment_plan
  return (
    <section className="card-limit-section">
      <h3>How you pay this card</h3>
      <p className="muted-note">
        {plan
          ? `You said each statement is paid in full by its due date (“${plan.reason}”). The forecast uses this.`
          : 'Not set. If your repayments can’t be matched to statements, the forecast leaves this card’s repayments out.'}
      </p>
      {!editing && <div className="row-actions">
        <Button variant="outline" size="sm" onClick={() => setEditing(true)}>{plan ? 'Remove plan' : 'I pay it in full each month'}</Button>
      </div>}
      {editing && <RepaymentPlanForm detail={detail} generation={generation}
        onSaved={async status => { setEditing(false); await onSaved(status) }} onCancel={() => setEditing(false)} />}
    </section>
  )
}

function CardDetailView({
  detail, cards, generation, onSaved, onBack,
}: { detail: CardDetail; cards: CardAccount[]; generation: string; onSaved: (status: string) => void | Promise<void>; onBack: () => void }) {
  const limit = detail.limit_basis as { value?: string; currency?: string; observed_on?: string; source_kind?: string } | null
  return (
    <Card className="debt-detail"><WorkspaceReportingDetails data={detail}/>
      <Button variant="ghost" size="sm" onClick={onBack}><ArrowLeft size={14} /> All cards</Button>
      <div className="debt-detail-head">
        <div>
          <h2>{detail.account.display_name}</h2>
          <p>
            {detail.account.masked_identifier}
            {detail.facility ? ` · facility ${detail.facility.label}` : ' · no shared facility'}
            {' · as of '}{detail.as_of}
          </p>
        </div>
        <div className="debt-detail-balance">
          <span>Limit basis</span>
          <strong>{limit?.value ? reportedMoney(limit, 'value', limit.currency ?? 'SGD') : 'Unavailable'}</strong>
          <small>
            {limit?.observed_on
              ? `observed ${limit.observed_on} · ${limit.source_kind ?? ''}`
              : 'from the latest statement print'}
          </small>
        </div>
      </div>
      <Utilization detail={detail} />
      <LimitAndFacility detail={detail} cards={cards} generation={generation} onSaved={onSaved} />
      <Repayments detail={detail} />
      <RepaymentPlan detail={detail} generation={generation} onSaved={onSaved} />
      {detail.coverage_gaps.map((g) => (
        <p key={g.from} className="debt-event-error">
          Missing statement period {g.from} … {g.to}
        </p>
      ))}
      {detail.manually_allocated_payments.length > 0 && (
        <div className="card-manual">
          <h3>Manually allocated payments</h3>
          <p className="muted-note">
            Operator-entered. Shown separately from any issuer-stated remaining due.
          </p>
          <ul>
            {detail.manually_allocated_payments.map((m, i) => (
              <li key={i}>
                {reportedMoney(m.amount, 'value', m.amount.currency ?? 'SGD')} → statement {m.statement_id}
                {m.bound ? '' : ' (unverified)'}
              </li>
            ))}
          </ul>
        </div>
      )}
      <h3>Statement activity</h3>
      {detail.statement_activity.length === 0 && (
        <p className="debt-empty">No selected statements.</p>
      )}
      {detail.statement_activity.map((s) => (
        <div key={s.statement_id} className="card-statement">
          <header>
            {s.period_start && s.period_end
              ? `${s.period_start} … ${s.period_end}`
              : 'Statement period not printed'}
            {' · '}closing {dateLabel(s.statement_date)}
          </header>
          {s.pockets.map((p) => (
            <SourceComparison key={p.currency} pocket={p} />
          ))}
        </div>
      ))}
      <p className="debt-detail-foot">
        “Statement closing balance” is an observed fact at period end. A timeless “current balance”
        is never shown; missing periods are listed before the picture is called complete.
      </p>
    </Card>
  )
}

export function CardsView({ generation, onSaved }: { generation: string; onSaved?: (status: string) => void | Promise<void> }) {
  const [page, setPage] = useState<CardsPage | null>(null)
  const [detail, setDetail] = useState<CardDetail | null>(null)
  const [openId, setOpenId] = useState<string | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let live = true
    setError('')
    loadCards({ generation })
      .then((p) => { if (live) setPage(p) })
      .catch((e) => { if (live) setError(e instanceof ApiError ? e.message : 'Could not load cards.') })
    return () => { live = false }
  }, [generation])

  // Reloads after a save too: a new generation re-reads the open card.
  useEffect(() => {
    if (!openId) { setDetail(null); return }
    let live = true
    setError('')
    loadCardDetail(openId, { generation })
      .then((d) => { if (live) setDetail(d) })
      .catch((e) => { if (live) setError(e instanceof ApiError ? e.message : 'Could not load that card.') })
    return () => { live = false }
  }, [openId, generation])

  if (error) return <Card className="debt-list"><p className="debt-empty">{error}</p></Card>
  if (openId && detail) return <CardDetailView detail={detail} cards={page?.cards.map(c => c.account) ?? [detail.account]}
    generation={generation} onSaved={async status => { await onSaved?.(status) }} onBack={() => setOpenId(null)} />
  if (!page) return <Card className="debt-list"><p className="debt-empty">Loading cards…</p></Card>

  return (
    <div className="debts-view"><WorkspaceReportingDetails data={page}/>
      <p className="debts-asof">Card balances as of {page.as_of}. Observed at each statement period end.</p>
      <Card className="debt-list">
        <header><h3>Cards</h3><p>One row per card account and currency.</p></header>
        {page.cards.length === 0 ? (
          <p className="debt-empty">No card statements imported yet.</p>
        ) : (
          page.cards.map((c) => (
            <CardRow key={c.account.account_id} row={c} onOpen={setOpenId} />
          ))
        )}
      </Card>
    </div>
  )
}
