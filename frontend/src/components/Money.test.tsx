import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ConsolidatedMoney, Money, RoundingReconciliationNote, type ValuedMoneyDTO } from './Money'

function valued(overrides: Partial<ValuedMoneyDTO> = {}): ValuedMoneyDTO {
  return {
    native: { value: '100.00', currency: 'USD' },
    reported: { value: '134.00', currency: 'SGD' },
    purpose: 'flow',
    basis: 'monthly_proxy',
    valuation_as_of: '2026-01-31',
    rate_as_of: null,
    rate_period: '2026-01',
    rate_path: [{
      base_currency: 'USD', quote_currency: 'SGD', period_month: '2026-01',
      effective_on: null, observation_kind: 'MONTHLY_PROXY', source: 'test',
      source_version: 'v1', inverted: false,
    }],
    forward_policy: null,
    unavailable: null,
    ...overrides,
  }
}

describe('Money', () => {
  it('renders the reported amount and currency, grouped, for an available valuation', () => {
    render(<Money value={valued({ reported: { value: '1234.00', currency: 'SGD' } })} />)
    expect(screen.getByText('1,234.00 SGD')).toBeInTheDocument()
  })

  it('renders native currency directly for identity, without a basis tag', () => {
    render(<Money value={valued({
      basis: 'identity', reported: { value: '10.00', currency: 'SGD' },
      native: { value: '10.00', currency: 'SGD' }, rate_period: null,
    })} />)
    expect(screen.getAllByText('10.00 SGD').length).toBeGreaterThan(0)
    expect(screen.queryByText(/rate period/i)).not.toBeInTheDocument()
  })

  it('does not pad a zero-scale currency like JPY to two decimal places', () => {
    render(<Money value={valued({ reported: { value: '300', currency: 'JPY' } })} />)
    expect(screen.getByText('300 JPY')).toBeInTheDocument()
  })

  it('does not truncate a three-decimal currency like KWD', () => {
    render(<Money value={valued({ reported: { value: '12.340', currency: 'KWD' } })} />)
    expect(screen.getByText('12.340 KWD')).toBeInTheDocument()
  })

  it('renders unavailable as an em dash plus a visible typed reason and remedy message', () => {
    render(<Money value={valued({
      reported: null, basis: null, rate_period: null,
      unavailable: { reason: 'RATE_NOT_IMPORTED', message: 'no fx rate configured for USD -> SGD' },
    })} />)
    expect(screen.getByText('RATE_NOT_IMPORTED')).toBeInTheDocument()
    expect(screen.getByText('no fx rate configured for USD -> SGD')).toBeInTheDocument()
  })

  it('marks a forward-held valuation distinguishably from a reference-valued one', () => {
    const { container } = render(<Money value={valued({ basis: 'held_latest' })} />)
    expect(container.querySelector('.money-forward')).not.toBeNull()
    expect(screen.getByText('Forward-held rate')).toBeInTheDocument()
  })

  it('exposes native amount, rate period, and source through a keyboard-reachable disclosure', () => {
    render(<Money value={valued()} />)
    // A native <summary> inside <details> is keyboard-focusable and toggleable
    // without JavaScript, unlike a hover-only tooltip.
    const summary = document.querySelector('.money-disclosure > summary')
    expect(summary).not.toBeNull()
    expect(summary?.tagName).toBe('SUMMARY')
    expect(screen.getByText('2026-01')).toBeInTheDocument()
    expect(screen.getByText(/test \(v1\)/)).toBeInTheDocument()
  })
})

describe('ConsolidatedMoney', () => {
  it('renders a complete total plainly', () => {
    render(<ConsolidatedMoney total={{ value: '500.00', currency: 'SGD' }} partialTotal={null} currency="SGD" />)
    expect(screen.getByText('500.00 SGD')).toBeInTheDocument()
    expect(screen.queryByText('Known subtotal')).not.toBeInTheDocument()
  })

  it('labels a partial total as a known subtotal, never as a total', () => {
    render(<ConsolidatedMoney total={null} partialTotal={{ value: '120.00', currency: 'SGD' }} currency="SGD" />)
    expect(screen.getByText('120.00 SGD')).toBeInTheDocument()
    expect(screen.getByText('Known subtotal')).toBeInTheDocument()
  })

  it('renders unavailable as an em dash when neither total nor partial total exists', () => {
    render(<ConsolidatedMoney total={null} partialTotal={null} currency="SGD" />)
    expect(screen.getByText('—')).toBeInTheDocument()
  })
})

describe('RoundingReconciliationNote', () => {
  it('renders a reconciliation note naming the delta', () => {
    render(<RoundingReconciliationNote delta={{ value: '0.01', currency: 'SGD' }} currency="SGD" />)
    expect(screen.getByText(/0.01 SGD/)).toBeInTheDocument()
  })
})
