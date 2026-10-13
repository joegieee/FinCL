import { reportedMoney } from '@/lib/workspaceReporting'
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
  onSelect,
}: {
  id: string
  label: string
  candidates: AllocationCandidate[]
  search: (query: string) => Promise<AllocationCandidate[]>
  value: string
  currency: string
  onSelect: (candidate: AllocationCandidate | null) => void
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
    onSelect(candidate)
    setQuery(sourceLabel(candidate))
    setOpen(false)
  }

  function clear() {
    setChosen(undefined)
    onSelect(null)
    setQuery('')
    setOpen(false)
  }

  const unresolved = Boolean(value) && !picked
  const inputRef = useRef<HTMLInputElement>(null)

  return (
    <div className="alloc-combobox">
      {/* The whole box is the hit target, not just the text inside it. */}
      <div
        className="alloc-combobox-input"
        onMouseDown={(e) => {
          if (e.target === inputRef.current) return
          e.preventDefault()
          inputRef.current?.focus()
        }}
      >
        {searching ? <Loader2 size={12} className="spin" /> : <Search size={12} />}
        <input
          ref={inputRef}
          id={id}
          role="combobox"
          aria-expanded={open}
          aria-autocomplete="list"
          aria-controls={`${id}-list`}
          aria-label={label}
          autoComplete="off"
          value={query}
          title={query}
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
      {picked && !open && (
        <p className="alloc-combobox-picked">
          {picked.effective_at.slice(0, 10)} · {reportedMoney(picked, 'amount', currency)}
        </p>
      )}
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
                  {c.effective_at.slice(0, 10)} · {reportedMoney(c, 'amount', currency)} · {reportedMoney(c, 'open_amount', currency)} open
                </span>
              </button>
            </li>
          ))}
          {!filtered.length && !searching && (
            <li className="alloc-combobox-empty">
              {!trimmed
                ? 'No suggestions yet. Type to search every transaction.'
                : trimmed.length < MIN_SEARCH_LENGTH
                ? `No transaction matches "${query}".`
                : `No transaction matching "${query}" found across the ledger.`}
            </li>
          )}
        </ul>
      )}
    </div>
  )
}

const CHARGE_FIELDS = ['interest_amount', 'fee_amount', 'separate_fee_amount'] as const
const CHARGE_LABEL = { interest_amount: 'Interest', fee_amount: 'Fee', separate_fee_amount: 'Separate fee' } as const

function hasCharges(row: AllocationInput): boolean {
  return CHARGE_FIELDS.some((f) => toMinor(row[f]) !== 0)
}

export function AllocationTable({
  debtId,
  eventKind,
  generation,
  currency,
  rows,
  onChange,
  eventPrincipal,
  sourceDirection,
  onUseTotal,
  known = [],
}: {
  debtId: string
  eventKind: string
  generation: string
  currency: string
  rows: AllocationInput[]
  /** `picked` is the transaction just chosen for a row, when that is what changed. */
  onChange: (rows: AllocationInput[], picked?: AllocationCandidate) => void
  eventPrincipal: string
  /** Which way the backing bank movement runs, to say what to pick. */
  sourceDirection?: 'IN' | 'OUT'
  /** Offered when the allocations do not match: adopt their total as the event principal. */
  onUseTotal?: (total: string) => void
  /** Sources already resolved by the caller (a seeded transaction), offered
   * ahead of the newest-first page so a pre-picked row shows its label from
   * the first render instead of reading as an unknown anchor. */
  known?: AllocationCandidate[]
}) {
  const [loaded, setLoaded] = useState<AllocationCandidate[]>([])
  const candidates = [...known, ...loaded.filter((c) => !known.some((k) => k.entry_anchor === c.entry_anchor))]
  const [note, setNote] = useState('')
  // Interest and fees are rare on informal debts, so their columns stay folded
  // away unless asked for -- or unless a row already carries one.
  const [chargesOpen, setChargesOpen] = useState(false)
  const showCharges = chargesOpen || rows.some(hasCharges)

  useEffect(() => {
    let live = true
    setNote('')
    loadAllocationCandidates({ debtId, eventKind, generation })
      .then((p) => { if (live) setLoaded(p.candidates) })
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

  function selectSource(index: number, candidate: AllocationCandidate | null) {
    onChange(
      rows.map((r, i) =>
        i === index
          ? candidate
            ? { ...r, entry_anchor: candidate.entry_anchor, principal_amount: candidate.open_amount }
            : { ...r, entry_anchor: '' }
          : r,
      ),
      candidate ?? undefined,
    )
  }

  const totalMinor = principalTotalMinor(rows)
  const wantMinor = toMinor(eventPrincipal)
  const balanced = Number.isFinite(totalMinor) && totalMinor === wantMinor
  const total = Number.isFinite(totalMinor) ? (totalMinor / 100).toFixed(2) : ''

  return (
    <div className="debt-alloc">
      <div className="debt-alloc-head">
        <span>Source transactions</span>
        <small>
          {sourceDirection === 'IN'
            ? 'The bank transfers that brought this money in.'
            : sourceDirection === 'OUT'
              ? 'The bank payments that sent this money out.'
              : 'borrowed / returned principal is a bank inflow; lent / repaid principal is an outflow'}
          {rows.length > 1 ? ' The event principal is their total.' : ''}
        </small>
      </div>
      {note ? <p className="debt-empty">{note}</p> : null}
      <table>
        <thead>
          <tr>
            <th>Transaction</th>
            <th className="num">Principal</th>
            {showCharges && CHARGE_FIELDS.map((f) => <th key={f} className="num">{CHARGE_LABEL[f]}</th>)}
            <th />
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>
              <td className="debt-alloc-source">
                <SourcePicker
                  id={`alloc-${index}-source`}
                  label={`Allocation ${index + 1} source`}
                  candidates={candidates}
                  search={search}
                  value={row.entry_anchor}
                  currency={currency}
                  onSelect={(candidate) => selectSource(index, candidate)}
                />
              </td>
              {(['principal_amount', ...(showCharges ? CHARGE_FIELDS : [])] as const).map((field) => (
                <td key={field} className="num">
                  <input
                    aria-label={`Allocation ${index + 1} ${field.replace('_amount', '')}`}
                    inputMode="decimal"
                    value={row[field]}
                    onChange={(e) => patch(index, field, e.target.value)}
                    onFocus={(e) => e.target.select()}
                  />
                </td>
              ))}
              <td className="debt-alloc-remove">
                <button
                  type="button"
                  aria-label={`Remove allocation ${index + 1}`}
                  title="Remove this source"
                  onClick={() => onChange(rows.filter((_, i) => i !== index))}
                >
                  <X size={14} />
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="debt-alloc-foot">
        <div className="debt-alloc-foot-actions">
          <button type="button" onClick={() => onChange([...rows, emptyAllocation()])}>
            <Plus size={13} /> Add a source
          </button>
          {!rows.some(hasCharges) && (
            <button type="button" className="link" onClick={() => setChargesOpen((o) => !o)} aria-expanded={showCharges}>
              {showCharges ? 'Hide interest & fees' : 'Interest & fees'}
            </button>
          )}
        </div>
        <span className={balanced ? 'ok' : 'warn'}>
          {balanced && totalMinor === 0 ? (
            <>Pick the transaction{rows.length > 1 ? 's' : ''} above</>
          ) : balanced ? (
            <>Allocated {money(total, currency)} · matches the event principal</>
          ) : (
            <>
              Allocated {money(total || '0', currency)} of {money(eventPrincipal, currency)} — must match the event principal
              {onUseTotal && totalMinor > 0 ? (
                <button type="button" className="link" onClick={() => onUseTotal(total)}>
                  Use {money(total, currency)}
                </button>
              ) : null}
            </>
          )}
        </span>
      </div>
    </div>
  )
}
