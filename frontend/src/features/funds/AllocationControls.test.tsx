import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const recordAllocation = vi.fn()
const retireAllocation = vi.fn()
vi.mock('./api', () => ({
  recordAllocation: (...a: unknown[]) => recordAllocation(...a),
  retireAllocation: (...a: unknown[]) => retireAllocation(...a),
}))
vi.mock('@/lib/api', async () => {
  class ApiError extends Error { status: number; constructor(status: number, message: string) { super(message); this.status = status } }
  return { money: (v: string, c = 'SGD') => `${c} ${v}`, ApiError }
})

const { AllocationControls } = await import('./AllocationControls')

function fund(over: Record<string, unknown> = {}) {
  return {
    fund: 'fund-1', name: 'Annual insurance', currency: 'SGD', status: 'underfunded',
    shortfall: '480.00',
    target: { available: true, amount_due: '480.00', due_on: '2026-12-12', amount_basis: 'observed_irregular_expense', date_basis: 'stated_due_date' },
    planned_contributions: { available: true, required_contribution: '160.00', remaining_dates: ['2026-10-12', '2026-11-12', '2026-12-12'], reason: null },
    recorded_allocation: { allocated_now: '0.00', entries: [] },
    insufficient_allocation: null,
    ...over,
  }
}

function renderControls(over: Record<string, unknown> = {}) {
  const onSaved = vi.fn()
  render(<AllocationControls fund={fund(over) as never} generation="gen-1" onSaved={onSaved} />)
  return onSaved
}

afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('AllocationControls', () => {
  it('records a contribution against the fund', async () => {
    recordAllocation.mockResolvedValue({ status: 'committed' })
    const onSaved = renderControls()

    fireEvent.change(screen.getByLabelText(/Amount set aside/), { target: { value: '160.00' } })
    fireEvent.change(screen.getByLabelText(/Date set aside/), { target: { value: '2026-10-12' } })
    fireEvent.click(screen.getByRole('button', { name: /Record contribution/ }))

    await waitFor(() => expect(recordAllocation).toHaveBeenCalledWith('gen-1', 'fund-1', {
      allocated_on: '2026-10-12', amount: '160.00',
      reason: 'set aside toward Annual insurance',
    }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('committed'))
  })

  it('refuses to record an empty contribution', async () => {
    renderControls()

    fireEvent.click(screen.getByRole('button', { name: /Record contribution/ }))

    expect(await screen.findByRole('alert')).toHaveTextContent('An amount is required.')
    expect(recordAllocation).not.toHaveBeenCalled()
  })

  it('lists recorded contributions and can withdraw one', async () => {
    retireAllocation.mockResolvedValue({ status: 'committed' })
    renderControls({
      recorded_allocation: {
        allocated_now: '160.00',
        entries: [{ allocation: 'alloc-1', allocated_on: '2026-10-12', amount: '160.00' }],
      },
    })

    fireEvent.click(screen.getByRole('button', { name: /Withdraw the 160.00 contribution/ }))

    await waitFor(() => expect(retireAllocation).toHaveBeenCalledWith(
      'gen-1', 'fund-1', 'alloc-1', 'this contribution was not actually set aside',
    ))
  })

  it('says plainly that a contribution moves no cash out', () => {
    renderControls()

    expect(screen.getByText(/total cash and every forecast balance stay/)).toBeInTheDocument()
  })
})
