import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { PeriodPicker } from './PeriodPicker'

const loadJourney = vi.hoisted(() => vi.fn())
vi.mock('@/features/journey/api', () => ({ loadJourney }))

describe('PeriodPicker', () => {
  beforeEach(() => { loadJourney.mockResolvedValue({ statements: { complete_through: '2026-08-20' } }) })
  afterEach(() => { cleanup(); vi.clearAllMocks() })

  async function choose(name: string) {
    const onApply = vi.fn()
    render(<PeriodPicker start="2026-07-01" end="2026-07-31" applying={false} onApply={onApply} />)
    await waitFor(() => expect(loadJourney).toHaveBeenCalled())
    await Promise.resolve()
    fireEvent.click(screen.getByRole('button', { name }))
    return onApply
  }

  it('ends a preset on the last day the statements reach, on local calendar dates', async () => {
    expect(await choose('Latest month')).toHaveBeenCalledWith('2026-08-01', '2026-08-20')
  })

  it('falls back to today when the statements cannot be read', async () => {
    loadJourney.mockRejectedValue(new Error('offline'))
    vi.useFakeTimers({ now: new Date(2026, 10, 1, 9), toFake: ['Date'] })
    expect(await choose('Latest month')).toHaveBeenCalledWith('2026-11-01', '2026-11-01')
    vi.useRealTimers()
  })
})
