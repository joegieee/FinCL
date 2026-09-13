import { useCallback, useEffect, useRef, useState } from 'react'
import { Loader2, Plus, Search, X } from 'lucide-react'
import { ApiError, money } from '@/lib/api'
import {
  loadAllocationCandidates,
  type AllocationCandidate,
  type AllocationInput,
} from './api'

const ZERO = '0.00'

export function emptyAllocation(anchor = ''): AllocationInput {
  return {
    entry_anchor: anchor,
    principal_amount: ZERO,
    interest_amount: ZERO,
    fee_amount: ZERO,
    separate_fee_amount: ZERO,
  }
}

function toMinor(value: string): number {
  const n = Number(value)
  return Number.isFinite(n) ? Math.round(n * 100) : NaN
}

/** Sum of the allocation principals, or NaN if any field is unparseable. */
export function principalTotalMinor(rows: AllocationInput[]): number {
  return rows.reduce((acc, r) => acc + toMinor(r.principal_amount), 0)
}

const SEARCH_DEBOUNCE_MS = 250
const MIN_SEARCH_LENGTH = 2

function sourceLabel(candidate: AllocationCandidate): string {
  return candidate.description?.trim() || candidate.entry_anchor
}

function matches(candidate: AllocationCandidate, query: string): boolean {
  const q = query.trim().toLowerCase()
  if (!q) return true
  return (
    (candidate.description ?? '').toLowerCase().includes(q) ||
    candidate.entry_anchor.toLowerCase().includes(q) ||
    candidate.effective_at.toLowerCase().includes(q) ||
    candidate.amount.includes(q)
  )
}

function SourcePicker({
  id,
  label,
  candidates,
  search,
  value,
  currency,
  onChange,
}: {
  id: string
  label: string
  candidates: AllocationCandidate[]
  search: (query: string) => Promise<AllocationCandidate[]>
  value: string
  currency: string
  onChange: (anchor: string) => void
}) {
  const initial = candidates.find((c) => c.entry_anchor === value)
  const [chosen, setChosen] = useState<AllocationCandidate | undefined>(initial)
  const [query, setQuery] = useState(initial ? sourceLabel(initial) : value)
  const [open, setOpen] = useState(false)
  const [highlight, setHighlight] = useState(0)
  const [remote, setRemote] = useState<AllocationCandidate[] | null>(null)
  const [searching, setSearching] = useState(false)
  const requestId = useRef(0)

  useEffect(() => {
    const trimmed = query.trim()
    if (trimmed.length < MIN_SEARCH_LENGTH) {
      setRemote(null)
      setSearching(false)
      return
    }
    setSearching(true)
    const thisRequest = ++requestId.current
    const timer = setTimeout(() => {
      search(trimmed)
        .then((results) => { if (requestId.current === thisRequest) setRemote(results) })
        .catch(() => { if (requestId.current === thisRequest) setRemote(null) })
        .finally(() => { if (requestId.current === thisRequest) setSearching(false) })
    }, SEARCH_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [query, search])

  const trimmed = query.trim()
  const filtered = trimmed.length >= MIN_SEARCH_LENGTH && remote
    ? remote
    : candidates.filter((c) => matches(c, query))
  const known = [...(chosen ? [chosen] : []), ...candidates, ...(remote ?? [])]
  const picked = known.find((c) => c.entry_anchor === value)

  function choose(candidate: AllocationCandidate) {
    setChosen(candidate)
    onChange(candidate.entry_anchor)
    setQuery(sourceLabel(candidate))
    setOpen(false)
  }

  function clear() {
    setChosen(undefined)
    onChange('')
    setQuery('')
    setOpen(false)
  }

  const unresolved = Boolean(value) && !picked

  return (
    <div className="alloc-combobox">
      <div className="alloc-combobox-input">
        {searching ? <Loader2 size={12} className="spin" /> : <Search size={12} />}
        <input
          id={id}
          role="combobox"
          aria-expanded={open}
          aria-autocomplete="list"
          aria-controls={`${id}-list`}
          aria-label={label}
          autoComplete="off"
          value={query}
          placeholder="Search all transactions by description…"
          onChange={(e) => { setQuery(e.target.value); setOpen(true); setHighlight(0) }}
          onFocus={() => setOpen(true)}
          onBlur={() => setTimeout(() => setOpen(false), 150)}
          onKeyDown={(e) => {
            if (e.key === 'ArrowDown') {
              e.preventDefault()
              setOpen(true)
              setHighlight((h) => Math.min(h + 1, Math.max(filtered.length - 1, 0)))
            } else if (e.key === 'ArrowUp') {
              e.preventDefault()
              setHighlight((h) => Math.max(h - 1, 0))
            } else if (e.key === 'Enter') {
              if (open && filtered[highlight]) { e.preventDefault(); choose(filtered[highlight]) }
            } else if (e.key === 'Escape' && open) {
              e.stopPropagation()
              setOpen(false)
            }
          }}
        />
      </div>
      {unresolved && <p className="alloc-combobox-unresolved">{value} (unresolved — no longer among candidates)</p>}
      {open && (
        <ul className="alloc-combobox-list" id={`${id}-list`} role="listbox">
          {value && (
            <li role="option" aria-selected={false}>
              <button type="button" className="alloc-combobox-clear" onMouseDown={(e) => e.preventDefault()} onClick={clear}>
                Clear selection
              </button>
            </li>
          )}
          {filtered.map((c, i) => (
            <li key={c.entry_anchor} role="option" aria-selected={c.entry_anchor === value}>
              <button
                type="button"
                className={i === highlight ? 'is-active' : ''}
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => choose(c)}
              >
                <span className="alloc-combobox-desc">{c.description?.trim() || 'No description'}</span>
                <span className="alloc-combobox-meta">
                  {c.effective_at.slice(0, 10)} · {money(c.amount, currency)} · {money(c.open_amount, currency)} open
                </span>
              </button>
            </li>
          ))}
          {!filtered.length && !searching && (
            <li className="alloc-combobox-empty">
              {trimmed.length < MIN_SEARCH_LENGTH
                ? `No transaction matches "${query}".`
                : `No transaction matching "${query}" found across the ledger.`}
            </li>
          )}
        </ul>
      )}
    </div>
  )
}

export function AllocationTable({
  debtId,
  eventKind,
  generation,
  currency,
  rows,
  onChange,
  eventPrincipal,
}: {
  debtId: string
  eventKind: string
  generation: string
  currency: string
  rows: AllocationInput[]
  onChange: (rows: AllocationInput[]) => void
  eventPrincipal: string
}) {
  const [candidates, setCandidates] = useState<AllocationCandidate[]>([])
  const [note, setNote] = useState('')

  useEffect(() => {
    let live = true
    setNote('')
    loadAllocationCandidates({ debtId, eventKind, generation })
      .then((p) => { if (live) setCandidates(p.candidates) })
      .catch((e) => {
        if (live) setNote(e instanceof ApiError ? e.message : 'Could not load source transactions.')
      })
    return () => { live = false }
  }, [debtId, eventKind, generation])

  const search = useCallback(
    (query: string) =>
      loadAllocationCandidates({ debtId, eventKind, generation, search: query, limit: 25 }).then((p) => p.candidates),
    [debtId, eventKind, generation],
  )

  function patch(index: number, field: keyof AllocationInput, value: string) {
    onChange(rows.map((r, i) => (i === index ? { ...r, [field]: value } : r)))
  }

  const totalMinor = principalTotalMinor(rows)
  const wantMinor = toMinor(eventPrincipal)
  const balanced = Number.isFinite(totalMinor) && totalMinor === wantMinor

  return (
    <div className="debt-alloc">
      <div className="debt-alloc-head">
        <span>Source transactions</span>
        <small>
          borrowed / returned principal is a bank inflow; lent / repaid principal is an outflow
        </small>
      </div>
      {note ? <p className="debt-empty">{note}</p> : null}
      <table>
        <thead>
          <tr>
            <th>Transaction</th><th>Principal</th><th>Interest</th>
            <th>Fee</th><th>Separate fee</th><th />
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>
              <td>
                <SourcePicker
                  id={`alloc-${index}-source`}
                  label={`Allocation ${index + 1} source`}
                  candidates={candidates}
                  search={search}
                  value={row.entry_anchor}
                  currency={currency}
                  onChange={(anchor) => patch(index, 'entry_anchor', anchor)}
                />
              </td>
              {(['principal_amount', 'interest_amount', 'fee_amount', 'separate_fee_amount'] as const).map(
                (field) => (
                  <td key={field}>
                    <input
                      aria-label={`Allocation ${index + 1} ${field.replace('_amount', '')}`}
                      inputMode="decimal"
                      value={row[field]}
                      onChange={(e) => patch(index, field, e.target.value)}
                    />
                  </td>
                ),
              )}
              <td>
                <button
                  type="button"
                  aria-label={`Remove allocation ${index + 1}`}
                  onClick={() => onChange(rows.filter((_, i) => i !== index))}
                >
                  <X size={13} />
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="debt-alloc-foot">
        <button type="button" onClick={() => onChange([...rows, emptyAllocation()])}>
          <Plus size={13} /> Add a source
        </button>
        <span className={balanced ? 'ok' : 'warn'}>
          Allocated principal {money((totalMinor / 100).toFixed(2), currency)} of{' '}
          {money(eventPrincipal, currency)}
          {balanced ? '' : ' — must match the event principal'}
        </span>
      </div>
    </div>
  )
}
