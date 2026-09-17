import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const recordMatch = vi.fn()
vi.mock('./api', () => ({ recordMatch: (...a: unknown[]) => recordMatch(...a) }))
vi.mock('@/lib/api', async () => {
  class ApiError extends Error { status: number; constructor(status: number, message: string) { super(message); this.status = status } }
  return { money: (v: string, c = 'SGD') => `${c} ${v}`, ApiError }
})

const { MatchQueue } = await import('./MatchQueue')

function candidate(over: Record<string, unknown> = {}) {
  return {
    movement_id: 'mv-1', on: '2026-04-03', amount: '3000.00', label: 'ACME PAYROLL',
    account: 'Revolut Current', amount_difference: '0.00', exact_amount: true,
    days_from_window: 0, match_kind: 'full' as const, suggested_amount: '3000.00',
    ...over,
  }
}

function suggestion(over: Record<string, unknown> = {}) {
  return {
    event_id: 'salary:2026-03', label: 'March salary', direction: 'inflow',
    status: 'missing', outstanding: '3000.00',
    window: { earliest: '2026-03-01', latest: '2026-03-05', precision: 'day' },
    candidates: [candidate()],
    rule: 'candidates are ranked by amount difference then distance from the issued window',
    ...over,
  }
}

function renderQueue(suggestions: unknown[] = [suggestion()]) {
  const onMatched = vi.fn()
  render(
    <MatchQueue
      suggestions={suggestions as never} snapshot="snap-1" generation="gen-1"
      currency="SGD" onMatched={onMatched}
    />,
  )
  return onMatched
}

afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('MatchQueue', () => {
  it('records the match the operator confirms, with the candidate’s own figures', async () => {
    recordMatch.mockResolvedValue({ status: 'committed' })
    const onMatched = renderQueue()

    fireEvent.click(screen.getByRole('button', { name: /Match ACME PAYROLL/ }))

    await waitFor(() => expect(recordMatch).toHaveBeenCalledWith('gen-1', 'snap-1', {
      event_id: 'salary:2026-03', movement_id: 'mv-1', matched_on: '2026-04-03',
      amount: '3000.00', match_kind: 'full',
      reason: 'matched ACME PAYROLL to March salary',
    }))
    await waitFor(() => expect(onMatched).toHaveBeenCalledWith('committed'))
  })

  it('states why each candidate was offered, so a wrong one is visibly wrong', () => {
    renderQueue([suggestion({
      candidates: [candidate({
        exact_amount: false, amount_difference: '120.00', days_from_window: 9,
        match_kind: 'partial', suggested_amount: '2880.00', amount: '2880.00',
      })],
    })])

    expect(screen.getByText(/SGD 120.00 from the outstanding amount/)).toBeInTheDocument()
    expect(screen.getByText(/9 days outside the issued window/)).toBeInTheDocument()
    expect(screen.getByText(/answer this event in part only/)).toBeInTheDocument()
  })

  it('shows the outstanding amount and the window the event was issued for', () => {
    renderQueue()

    expect(screen.getByText(/SGD 3000.00 outstanding/)).toBeInTheDocument()
    expect(screen.getByText(/2026-03-01 → 2026-03-05/)).toBeInTheDocument()
  })

  it('says plainly when nothing is waiting, rather than rendering an empty list', () => {
    renderQueue([])

    expect(screen.getByText(/No issued event is waiting on a match/)).toBeInTheDocument()
  })

  it('surfaces a rejected match instead of silently doing nothing', async () => {
    const { ApiError } = await import('@/lib/api')
    recordMatch.mockRejectedValue(
      new (ApiError as unknown as { new (s: number, m: string): Error })(409, 'the ledger moved on'),
    )
    renderQueue()

    fireEvent.click(screen.getByRole('button', { name: /Match ACME PAYROLL/ }))

    expect(await screen.findByRole('alert')).toHaveTextContent('the ledger moved on')
  })
})
