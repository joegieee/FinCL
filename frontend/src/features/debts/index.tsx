import { useEffect, useState } from 'react'
import { ArrowLeft, ChevronRight } from 'lucide-react'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { ApiError, money } from '@/lib/api'
import {
  RECONCILIATION_LABEL,
  loadDebtDetail,
  loadDebts,
  type DebtCard,
  type DebtDetail,
  type DebtsPage,
} from './api'

function dateLabel(value: string) {
  return new Date(value + 'T12:00:00').toLocaleDateString('en-SG', {
    month: 'short', day: 'numeric', year: 'numeric',
  })
}

function DebtRow({ debt, onOpen }: { debt: DebtCard; onOpen: (id: string) => void }) {
  return (
    <button className="debt-row" onClick={() => onOpen(debt.debt_id)}>
      <span className="debt-row-main">
        <strong>{debt.counterparty_label}</strong>
        <small>
          {debt.reconciliation_status === 'opening_balance_only'
            ? 'Opening balance'
            : RECONCILIATION_LABEL[debt.reconciliation_status] ?? debt.reconciliation_status}
          {debt.due_on ? ` · due ${dateLabel(debt.due_on)}` : ''}
        </small>
      </span>
      <span className="debt-row-amount">
        {money(debt.remaining_principal.value, debt.currency)}
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

function DebtDetailView({
  detail, onBack,
}: { detail: DebtDetail; onBack: () => void }) {
  const debt = detail.debt
  return (
    <Card className="debt-detail">
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
          <strong>{money(debt.remaining_principal.value, debt.currency)}</strong>
          <small>as of {detail.as_of}</small>
        </div>
      </div>
      <p className="debt-detail-status">
        {RECONCILIATION_LABEL[debt.reconciliation_status] ?? debt.reconciliation_status}
      </p>
      <table className="debt-history">
        <thead>
          <tr><th>Date</th><th>Event</th><th>Principal</th><th>Note</th></tr>
        </thead>
        <tbody>
          {debt.principal_history.map((e) => (
            <tr key={e.event_id}>
              <td>{dateLabel(e.effective_on)}</td>
              <td>
                {e.event_kind}
                {e.opening_balance ? <span className="tag">opening</span> : null}
                {!e.bound ? <span className="tag warn">unverified</span> : null}
              </td>
              <td>{(e.signed < 0 ? '−' : '+') + money(e.principal.value, debt.currency).replace(/^−/, '')}</td>
              <td>{e.reason}</td>
            </tr>
          ))}
          {debt.principal_history.length === 0 && (
            <tr><td colSpan={4} className="debt-empty">No principal events recorded.</td></tr>
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

export function DebtsView({ generation }: { generation: string }) {
  const [page, setPage] = useState<DebtsPage | null>(null)
  const [detail, setDetail] = useState<DebtDetail | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let live = true
    setError('')
    loadDebts({ generation })
      .then((p) => { if (live) setPage(p) })
      .catch((e) => { if (live) setError(e instanceof ApiError ? e.message : 'Could not load debts.') })
    return () => { live = false }
  }, [generation])

  function openDetail(id: string) {
    setError('')
    loadDebtDetail(id, { generation })
      .then(setDetail)
      .catch((e) => setError(e instanceof ApiError ? e.message : 'Could not load that debt.'))
  }

  if (error) return <Card className="debt-list"><p className="debt-empty">{error}</p></Card>
  if (detail) return <DebtDetailView detail={detail} onBack={() => setDetail(null)} />
  if (!page) return <Card className="debt-list"><p className="debt-empty">Loading debts…</p></Card>

  return (
    <div className="debts-view">
      <p className="debts-asof">Balances as of {page.as_of}. Payables and receivables shown separately.</p>
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
