import { useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { api, money } from '@/lib/api'
import { useLedgerCommand } from '@/lib/command'
import { CategoryPicker } from '@/features/classification/CategoryPicker'

type OwnerShare = {
  generation: string
  entry_anchor: string
  currency: string
  amount: string
  available_amount: string
  unassigned_amount: string
  owner_share: { expense_amount: string; expense_category: string; reason: string } | null
}

// Compare native decimal inputs exactly, including amounts beyond JS's safe
// integer range. The server remains the authority for the complete partition.
function units(text: string): bigint | null {
  const match = /^(\d+)(?:\.(\d{1,9}))?$/.exec(text.trim())
  return match ? BigInt(match[1] + (match[2] ?? '').padEnd(9, '0')) : null
}

/** The owner share belongs to the source transaction, even when several debts
 * share it. Replacing it must not append a second expense or change principal. */
export function OwnerShareForm({ anchor, generation, categories, onSaved, onCancel }: {
  anchor: string
  generation: string
  categories: string[]
  onSaved: (status: string) => void | Promise<void>
  onCancel: () => void
}) {
  const [source, setSource] = useState<OwnerShare | null>(null)
  const [loadError, setLoadError] = useState('')
  const [amount, setAmount] = useState('')
  const [category, setCategory] = useState('')
  const [reason, setReason] = useState('')
  const [removing, setRemoving] = useState(false)
  const [problem, setProblem] = useState('')
  const cmd = useLedgerCommand(generation, onSaved)

  useEffect(() => {
    let live = true
    const query = new URLSearchParams({ anchor, generation })
    api<OwnerShare>('debts/entry-residual?' + query)
      .then(value => {
        if (!live) return
        setSource(value)
        setAmount(value.owner_share?.expense_amount ?? '')
        setCategory(value.owner_share?.expense_category ?? '')
      })
      .catch(error => { if (live) setLoadError((error as Error).message) })
    return () => { live = false }
  }, [anchor, generation])

  const disabled = cmd.busy || cmd.attempted
  function edit(change: () => void) { change(); setProblem(''); cmd.reset() }

  function submit() {
    if (!source) return
    const value = units(amount)
    const limit = units(source.available_amount)
    const error = !reason.trim() ? 'A reason is required.'
      : !removing && (value === null || value <= 0n) ? 'Enter your share as a positive amount with at most 9 decimals.'
      : !removing && limit !== null && value !== null && value > limit ? 'Your share exceeds the amount left after other allocations.'
      : !removing && !category ? 'Choose a category for your share.' : ''
    if (error) { setProblem(error); return }
    void cmd.act({
      kind: removing ? 'ENTRY_RESIDUAL_RETIRE' : 'ENTRY_RESIDUAL_SET',
      anchors: { entry: anchor },
      payload: removing ? { reason: reason.trim() }
        : { expense_amount: amount.trim(), expense_category: category, reason: reason.trim() },
    })
  }

  return <section className="debt-event-form" aria-label="Your share of this transaction">
    <h3>Your expense share</h3>
    {loadError && <p className="form-error" role="alert">{loadError}</p>}
    {!source && !loadError && <p className="muted-note">Loading allocation details…</p>}
    {source && <>
      <p className="muted-note">Amounts are in the transaction’s original currency, {source.currency}.
        {' '}The source amount is {money(source.amount, source.currency)}; up to {money(source.available_amount, source.currency)} is available for your share after other allocations.
        {' '}Currently unassigned: {money(source.unassigned_amount, source.currency)}. Debt principal and separate source fees stay as recorded.</p>
      {source.owner_share && <p className="muted-note">Recorded share: {money(source.owner_share.expense_amount, source.currency)} · {source.owner_share.expense_category}.
        {' '}Reason: {source.owner_share.reason}</p>}
      {source.owner_share && <label><input type="checkbox" checked={removing} disabled={disabled}
        onChange={e => edit(() => setRemoving(e.target.checked))}/> Remove my recorded share</label>}
      {removing ? <p className="muted-note">This removes the explicit expense share. The released amount returns to the transaction’s ordinary classification; it does not reduce any debt balance.</p>
        : <>
          <label>Your share ({source.currency})
            <input aria-label={`Your share (${source.currency})`} inputMode="decimal" value={amount} disabled={disabled}
              onChange={e => edit(() => setAmount(e.target.value))}/>
          </label>
          <CategoryPicker id={`owner-share-${anchor}`} label="Your share category" categories={categories.filter(c => !c.startsWith('Unclassified /'))}
            value={category} disabled={disabled} onChange={value => edit(() => setCategory(value))}/>
        </>}
      <label>Reason for this change
        <textarea aria-label="Reason for this change" value={reason} disabled={disabled} rows={2}
          onChange={e => edit(() => setReason(e.target.value))}/>
      </label>
      {(problem || cmd.error) && <p className="form-error" role="alert">{problem || cmd.error}</p>}
      {cmd.previewed && <p role="status">Checked. {removing ? 'Remove your recorded expense share' : `Record ${money(amount, source.currency)} as your expense under ${category}`}.</p>}
      <div className="debt-event-actions">
        <Button type="button" variant="ghost" size="sm" disabled={disabled} onClick={onCancel}>Cancel</Button>
        <Button type="button" size="sm" disabled={cmd.busy} onClick={submit}>
          {cmd.busy ? 'Working…' : cmd.attempted ? 'Retry save' : cmd.previewed ? 'Save share' : 'Check share'}
        </Button>
      </div>
    </>}
    {loadError && <Button type="button" variant="ghost" size="sm" onClick={onCancel}>Back</Button>}
  </section>
}
