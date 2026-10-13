import { reportedMoney } from '@/lib/workspaceReporting'
import { useCallback, useEffect, useState } from 'react'
import { Search } from 'lucide-react'
import { ApiError, money } from '@/lib/api'
import { loadEntryCandidates, type EntryCandidate } from './api'

/**
 * Pick the source transaction behind a purchase attribution or a settlement.
 *
 * Every row shows both the entry's full amount and what is still unclaimed. They
 * differ once part of an entry is spoken for elsewhere, and the unclaimed figure
 * is the one that bounds a new allocation — showing only the gross amount would
 * invite an over-claim the backend would then refuse.
 */
export function EntryPicker({
  planId,
  purpose,
  generation,
  currency,
  selected,
  onPick,
}: {
  planId: string
  purpose: 'purchase' | 'settlement'
  generation: string
  currency: string
  selected: string | null
  onPick: (candidate: EntryCandidate) => void
}) {
  const [search, setSearch] = useState('')
  const [rows, setRows] = useState<EntryCandidate[] | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(
    async (term: string) => {
      setBusy(true)
      setError('')
      try {
        const page = await loadEntryCandidates({
          planId,
          purpose,
          generation,
          search: term || undefined,
        })
        setRows(page.candidates)
      } catch (e) {
        setError(e instanceof ApiError ? e.message : 'Could not load transactions.')
      } finally {
        setBusy(false)
      }
    },
    [planId, purpose, generation],
  )

  useEffect(() => { void load('') }, [load])

  return (
    <div className="entry-picker">
      <div className="entry-picker-search">
        <Search size={13} />
        <input
          aria-label="Search transactions"
          placeholder={
            purpose === 'purchase'
              ? 'Search charges on this card…'
              : 'Search the payment that settled this plan…'
          }
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); void load(search) } }}
        />
      </div>

      {error ? <p className="debt-event-error">{error}</p> : null}

      {rows === null ? (
        <p className="muted-note">Loading transactions…</p>
      ) : rows.length === 0 ? (
        <p className="muted-note">
          {purpose === 'purchase'
            ? 'No unclaimed charges on this card match. Every candidate may already be attributed elsewhere.'
            : 'No unclaimed outgoing transactions match.'}
        </p>
      ) : (
        <table className="debt-history entry-picker-table">
          <thead>
            <tr><th/><th>Date</th><th>Transaction</th><th>Amount</th><th>Unclaimed</th></tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr
                key={row.entry_anchor}
                className={selected === row.entry_anchor ? 'is-selected' : undefined}
              >
                <td>
                  <input
                    type="radio"
                    name="entry-picker"
                    aria-label={`Use ${row.description ?? row.entry_anchor}`}
                    checked={selected === row.entry_anchor}
                    onChange={() => onPick(row)}
                    disabled={busy}
                  />
                </td>
                <td>{row.effective_at.slice(0, 10)}</td>
                <td>{row.description ?? '—'}</td>
                <td>{reportedMoney(row, 'amount', currency)}</td>
                <td>
                  {reportedMoney(row, 'open_amount', currency)}
                  {row.open_amount !== row.amount ? <small> (partly claimed)</small> : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
