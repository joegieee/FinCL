import { reportedMoney } from '@/lib/workspaceReporting'
import { useEffect, useRef, useState } from 'react'
import { Search } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { money } from '@/lib/api'
import { CategoryPicker, rememberCategory } from '@/features/classification/CategoryPicker'
import { DraftEffect, useDebtDraft } from './Draft'
import {
  loadSplitCandidates,
  splitItems,
  type AllocationCandidate,
  type DebtCard,
} from './api'

type ShareRow = { key: string; who: string; name: string; amount: string }

const NEW = 'new'
const blankShare = (): ShareRow => ({ key: crypto.randomUUID(), who: '', name: '', amount: '' })

/** Split one bill I paid with other people, in one atomic batch.
 *
 * Each other person's share becomes money they owe me (an existing receivable,
 * or a new one opened with the split); whatever is left is my own share and is
 * spending under the category I choose. The server works out that share when
 * the split is checked, so the shares always add up to the whole unclaimed
 * amount and the bill stops asking to be categorized as a whole. */
export function SplitBillForm({
  generation,
  categories,
  receivables,
  onSaved,
  onCancel,
}: {
  generation: string
  categories: string[]
  /** Debts owed to me; the open ones in the bill's currency are offered. */
  receivables: DebtCard[]
  onSaved: (status: string) => void | Promise<void>
  onCancel: () => void
}) {
  const [search, setSearch] = useState('')
  const [bills, setBills] = useState<AllocationCandidate[] | null>(null)
  const [loadError, setLoadError] = useState('')
  const [bill, setBill] = useState<AllocationCandidate | null>(null)
  const [shares, setShares] = useState<ShareRow[]>([blankShare()])
  const [category, setCategory] = useState('')
  const [reason, setReason] = useState('')
  const [problem, setProblem] = useState('')
  const savedCategory = useRef('')
  const draft = useDebtDraft(generation, async (status) => {
    if (savedCategory.current) rememberCategory(savedCategory.current)
    await onSaved(status)
  })

  useEffect(() => {
    let live = true
    const timer = setTimeout(() => {
      loadSplitCandidates({ generation, search: search.trim() || undefined })
        .then((page) => { if (live) { setBills(page.candidates); setLoadError('') } })
        .catch((e) => { if (live) setLoadError((e as Error).message) })
    }, search ? 250 : 0)
    return () => { live = false; clearTimeout(timer) }
  }, [generation, search])

  function edit(next: () => void) { next(); setProblem(''); draft.invalidate() }
  function pick(row: AllocationCandidate) {
    edit(() => {
      setBill(row)
      setShares([blankShare()])
      if (!reason.trim()) setReason(`Split ${row.description ?? 'a bill'}`)
    })
  }
  function updateShare(key: string, change: Partial<ShareRow>) {
    edit(() => setShares((rows) => rows.map((r) => (r.key === key ? { ...r, ...change } : r))))
  }

  const mine = draft.preview?.items.find((i) => i.kind === 'ENTRY_RESIDUAL_SET')?.payload
  const offered = bill
    ? receivables.filter((d) => d.status === 'OPEN' && d.currency === bill.currency && d.opened_on <= bill.effective_at.slice(0, 10))
    : []

  function submit() {
    if (!bill) return
    if (draft.preview) { void draft.save(reason.trim()); return }
    const chosen = shares.filter((s) => s.who)
    const invalid =
      !chosen.length ? 'Choose who shared the bill.'
      : shares.some((s) => !s.who) ? 'Choose a person for every share, or remove the empty one.'
      : chosen.some((s) => s.who === NEW && !s.name.trim()) ? 'Name each new person.'
      : new Set(chosen.filter((s) => s.who !== NEW).map((s) => s.who)).size !== chosen.filter((s) => s.who !== NEW).length ? 'Each person appears once; add their shares together.'
      : chosen.some((s) => !s.amount.trim()) ? 'Enter each person’s share.'
      : !reason.trim() ? 'A reason is required.'
      : ''
    if (invalid) { setProblem(invalid); return }
    savedCategory.current = category
    void draft.check(splitItems(
      bill,
      chosen.map((s) => ({ debtId: s.who === NEW ? null : s.who, name: s.name, amount: s.amount })),
      category,
      reason.trim(),
    ))
  }

  return (
    <div className="debt-event-form split-bill-form">
      <h3>Split a bill</h3>
      <p className="muted-note">
        Pick a bill you paid, then say who owes you their share. What is left is your own share and counts as your spending.
      </p>

      {!bill ? (
        <div className="entry-picker">
          <div className="entry-picker-search">
            <Search size={13} />
            <input aria-label="Find the bill" placeholder="Search your payments…" value={search} onChange={(e) => setSearch(e.target.value)} />
          </div>
          {loadError ? <p className="debt-event-error">{loadError}</p> : null}
          {bills === null ? (
            <p className="muted-note">Loading payments…</p>
          ) : bills.length === 0 ? (
            <p className="muted-note">No payment matches. A bill already split, or fully recorded against a debt, is not offered again.</p>
          ) : (
            <table className="debt-history entry-picker-table">
              <thead><tr><th>Date</th><th>Payment</th><th>Amount</th><th /></tr></thead>
              <tbody>
                {bills.map((row) => (
                  <tr key={row.entry_anchor}>
                    <td>{row.effective_at.slice(0, 10)}</td>
                    <td>{row.description ?? '—'}</td>
                    <td>
                      {reportedMoney(row, 'open_amount', row.currency)}
                      {row.open_amount !== row.amount ? <small> of {reportedMoney(row, 'amount', row.currency)} unclaimed</small> : null}
                    </td>
                    <td><Button type="button" size="sm" variant="outline" onClick={() => pick(row)}>Split this</Button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      ) : (
        <>
          <div className="split-bill-picked">
            <span>{bill.effective_at.slice(0, 10)} · {bill.description ?? 'Payment'}</span>
            <strong>{reportedMoney(bill, 'open_amount', bill.currency)}</strong>
            <Button type="button" size="sm" variant="ghost" onClick={() => edit(() => setBill(null))} disabled={draft.busy}>Change bill</Button>
          </div>

          {shares.map((share, i) => (
            <div key={share.key} className="debt-event-grid split-bill-share">
              <label>
                Who owes you
                <select aria-label={`Person ${i + 1}`} value={share.who} onChange={(e) => updateShare(share.key, { who: e.target.value })} disabled={draft.busy}>
                  <option value="">Choose…</option>
                  {offered.map((d) => <option key={d.debt_id} value={d.debt_id}>{d.counterparty_label}</option>)}
                  <option value={NEW}>Someone new…</option>
                </select>
              </label>
              {share.who === NEW ? (
                <label>
                  Name
                  <input aria-label={`Person ${i + 1} name`} value={share.name} onChange={(e) => updateShare(share.key, { name: e.target.value })} disabled={draft.busy} />
                </label>
              ) : null}
              <label>
                Their share ({bill.currency})
                <input aria-label={`Person ${i + 1} share`} inputMode="decimal" value={share.amount} onChange={(e) => updateShare(share.key, { amount: e.target.value })} disabled={draft.busy} />
              </label>
              {shares.length > 1 ? (
                <Button type="button" size="sm" variant="ghost" aria-label={`Remove person ${i + 1}`} onClick={() => edit(() => setShares((rows) => rows.filter((r) => r.key !== share.key)))} disabled={draft.busy}>Remove</Button>
              ) : null}
            </div>
          ))}
          <div>
            <Button type="button" size="sm" variant="outline" onClick={() => edit(() => setShares((rows) => [...rows, blankShare()]))} disabled={draft.busy}>Add another person</Button>
          </div>

          <CategoryPicker
            id="split-bill-category"
            label="Category for your share"
            categories={categories.filter((c) => !c.startsWith('Unclassified /'))}
            value={category}
            onChange={(c) => edit(() => setCategory(c))}
            disabled={draft.busy}
          />
          <p className="muted-note">What the shares leave of the bill is your own share. Not needed if they cover all of it.</p>
          {draft.preview ? (
            <p className="split-bill-mine" role="status">
              {mine && typeof mine.expense_amount === 'string'
                ? `Your share: ${money(mine.expense_amount, bill.currency)}`
                : 'You kept no share of this bill: all of it is owed back to you.'}
            </p>
          ) : null}
          {draft.preview ? <DraftEffect preview={draft.preview} /> : null}

          <label className="debt-event-reason">
            Reason
            <textarea aria-label="Reason" rows={2} value={reason} onChange={(e) => edit(() => setReason(e.target.value))} disabled={draft.busy} />
          </label>
        </>
      )}

      {problem || draft.error ? <p className="debt-event-error" role="alert">{problem || draft.error}</p> : null}
      <div className="debt-event-actions">
        <Button variant="ghost" size="sm" onClick={onCancel} disabled={draft.busy}>Cancel</Button>
        {bill ? <Button size="sm" onClick={submit} disabled={draft.busy}>
          {draft.busy ? 'Working…' : draft.preview ? (draft.retrying ? 'Retry save' : 'Save split') : 'Check split'}
        </Button> : null}
      </div>
    </div>
  )
}
