import { useState } from 'react'
import { ApiError, money } from '@/lib/api'
import { dateLabel } from '@/lib/format'
import { useBatchCommand } from '@/lib/command'
import { kindLabel, previewDebtDraft, type DraftItem, type DraftPreview } from './api'

/**
 * Check, then save, a debt draft. `check` asks the server what the draft would
 * save and do to every balance; `save` records exactly the items that check
 * returned, as one batch, retry-safe. Any edit to the draft must call
 * `invalidate` so a stale check is never saved.
 */
export function useDebtDraft(generation: string, onSaved: (status: string) => void | Promise<void>) {
  const [preview, setPreview] = useState<DraftPreview | null>(null)
  const [checking, setChecking] = useState(false)
  const [checkError, setCheckError] = useState('')
  const batch = useBatchCommand(generation, async status => { setPreview(null); await onSaved(status) })

  async function check(items: DraftItem[]) {
    if (checking) return
    setChecking(true); setCheckError('')
    try {
      setPreview(await previewDebtDraft(generation, items))
    } catch (e) {
      setCheckError(e instanceof ApiError ? e.message : 'Could not check this change.')
    } finally {
      setChecking(false)
    }
  }

  async function save(reason: string) {
    if (preview) await batch.submit(preview.items, reason)
  }

  function invalidate() {
    if (batch.busy) return
    setPreview(null); setCheckError(''); batch.invalidate()
  }

  return {
    preview, check, save, invalidate,
    busy: checking || batch.busy,
    error: checkError || batch.error,
    /** A save whose outcome is unknown; saving again resends the same request. */
    retrying: Boolean(batch.submitted && batch.error),
  }
}

const CHANGE_LABEL = { added: 'new', changed: 'changed', balance: 'balance changes' } as const

/** What a checked draft does to each debt: every event with the principal
 * standing after it, before and after the change. */
export function DraftEffect({ preview }: { preview: DraftPreview }) {
  return <div className="debt-draft-effect" role="status">
    {preview.debts.map(debt => {
      const status = debt.before && debt.before.status !== debt.after.status
        ? ` · ${debt.before.status.toLowerCase()} → ${debt.after.status.toLowerCase()}`
        : debt.after.status === 'CLOSED' ? ` · stays closed${debt.after.closed_on ? ` on ${dateLabel(debt.after.closed_on)}` : ''}` : ''
      return <div key={debt.debt_id}>
        <p>
          <strong>{debt.counterparty_label}</strong>
          {' · '}remaining {debt.before ? <>{money(debt.before.remaining, debt.currency)} → </> : null}
          {money(debt.after.remaining, debt.currency)}{status}
        </p>
        <table className="debt-history">
          <thead><tr><th>Date</th><th>Event</th><th>Principal</th><th>Balance after</th><th /></tr></thead>
          <tbody>
            {debt.events.map(event => <tr key={event.event_id} className={event.change ? 'is-changed' : undefined}>
              <td>{dateLabel(event.effective_on)}</td>
              <td>{kindLabel(event.event_kind)}</td>
              <td>{event.signed < 0 ? '−' : '+'}{money(event.principal, debt.currency)}</td>
              <td>
                {event.balance_before !== null && event.change ? <><s>{money(event.balance_before, debt.currency)}</s>{' '}</> : null}
                {money(event.balance, debt.currency)}
              </td>
              <td>{event.change ? <span className="tag">{CHANGE_LABEL[event.change]}</span> : null}</td>
            </tr>)}
            {debt.removed.map(event => <tr key={event.event_id} className="is-removed">
              <td><s>{dateLabel(event.effective_on)}</s></td>
              <td><s>{kindLabel(event.event_kind)}</s></td>
              <td><s>{event.signed < 0 ? '−' : '+'}{money(event.principal, debt.currency)}</s></td>
              <td />
              <td><span className="tag warn">removed</span></td>
            </tr>)}
          </tbody>
        </table>
      </div>
    })}
  </div>
}
