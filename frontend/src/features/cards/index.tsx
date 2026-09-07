import { useEffect, useState } from 'react'
import { ArrowLeft, ChevronRight } from 'lucide-react'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { ApiError, money } from '@/lib/api'
import { SourceComparison } from './SourceComparison'
import {
  RECONCILIATION_LABEL,
  loadCardDetail,
  loadCards,
  type CardDetail,
  type CardListRow,
  type CardsPage,
} from './api'

function dateLabel(value: string) {
  return new Date(value + 'T12:00:00').toLocaleDateString('en-SG', {
    month: 'short', day: 'numeric', year: 'numeric',
  })
}

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
          ? money(latest.closing_liability.value, latest.closing_liability.currency ?? 'SGD')
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
        Credit balance of {money(u.credit_balance_amount ?? '0', u.currency)} — nothing is owed.
      </p>
    )
  }
  const width = Math.min(100, Math.max(0, Number(u.ratio) * 100))
  return (
    <div className="card-util">
      <div className="card-util-bar"><span style={{ width: `${width}%` }} /></div>
      <small>
        {u.basis} · ratio {u.ratio}
        {u.over_limit ? ` · over limit by ${money(u.over_limit_amount ?? '0', u.currency)}` : ` · ${money(u.headroom ?? '0', u.currency)} headroom`}
      </small>
    </div>
  )
}

function CardDetailView({
  detail, onBack,
}: { detail: CardDetail; onBack: () => void }) {
  const limit = detail.limit_basis as { value?: string; currency?: string; observed_on?: string; source_kind?: string } | null
  return (
    <Card className="debt-detail">
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
          <strong>{limit?.value ? money(limit.value, limit.currency ?? 'SGD') : 'Unavailable'}</strong>
          <small>
            {limit?.observed_on
              ? `observed ${limit.observed_on} · ${limit.source_kind ?? ''}`
              : 'from the latest statement print'}
          </small>
        </div>
      </div>
      <Utilization detail={detail} />
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
                {money(m.amount.value, m.amount.currency ?? 'SGD')} → statement {m.statement_id}
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

export function CardsView({ generation }: { generation: string }) {
  const [page, setPage] = useState<CardsPage | null>(null)
  const [detail, setDetail] = useState<CardDetail | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let live = true
    setError('')
    loadCards({ generation })
      .then((p) => { if (live) setPage(p) })
      .catch((e) => { if (live) setError(e instanceof ApiError ? e.message : 'Could not load cards.') })
    return () => { live = false }
  }, [generation])

  function openDetail(id: string) {
    setError('')
    loadCardDetail(id, { generation })
      .then(setDetail)
      .catch((e) => setError(e instanceof ApiError ? e.message : 'Could not load that card.'))
  }

  if (error) return <Card className="debt-list"><p className="debt-empty">{error}</p></Card>
  if (detail) return <CardDetailView detail={detail} onBack={() => setDetail(null)} />
  if (!page) return <Card className="debt-list"><p className="debt-empty">Loading cards…</p></Card>

  return (
    <div className="debts-view">
      <p className="debts-asof">Card balances as of {page.as_of}. Observed at each statement period end.</p>
      <Card className="debt-list">
        <header><h3>Cards</h3><p>One row per card account and currency.</p></header>
        {page.cards.length === 0 ? (
          <p className="debt-empty">No card statements imported yet.</p>
        ) : (
          page.cards.map((c) => (
            <CardRow key={c.account.account_id} row={c} onOpen={openDetail} />
          ))
        )}
      </Card>
    </div>
  )
}
