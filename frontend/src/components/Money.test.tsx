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

  it('renders unavailable as an em dash plus a visible typed reason and a remedy', () => {
    render(<Money value={valued({
      reported: null, basis: null, rate_period: null,
      unavailable: {
        reason: 'RATE_NOT_IMPORTED',
        message: 'no fx rate configured for USD -> SGD',
        remedy: 'Import an fx rate for USD -> SGD covering 2026-01.',
      },
    })} />)
    expect(screen.getByText('RATE_NOT_IMPORTED')).toBeInTheDocument()
    expect(screen.getByText('no fx rate configured for USD -> SGD')).toBeInTheDocument()
    // §2 invariant 8 is discharged by a reason *and a remedy*: a typed code
    // alone does not tell the reader what to do about the gap.
    expect(screen.getByText('Import an fx rate for USD -> SGD covering 2026-01.')).toBeInTheDocument()
  })

  it('never renders an unavailable valuation as a zero amount', () => {
    render(<Money value={valued({
      reported: null, basis: null, rate_period: null,
      unavailable: { reason: 'PERIOD_UNKNOWN', message: 'no precision', remedy: 'Record a date.' },
    })} />)
    expect(screen.queryByText(/\b0(\.00)?\b/)).not.toBeInTheDocument()
  })

  it.each([
    ['identity', 'Native currency'],
    ['realized', 'Realized rate'],
    ['exact_observation', 'Dated rate'],
    ['prior_observation', 'Prior dated rate'],
    ['monthly_average', 'Monthly average rate'],
    ['monthly_proxy', 'Monthly proxy rate'],
    ['current_mark', 'Current rate'],
    ['held_latest', 'Forward-held rate'],
  ] as const)('labels the %s basis distinguishably', (basis, label) => {
    // §2 invariant 4 / §9.3: every RateBasis variant must be reachable and
    // correctly labelled, not just the two M2 happens to resolve.
    render(<Money value={valued({ basis })} />)
    if (basis === 'identity') {
      // Identity applied no rate, so it carries no basis tag by design.
      expect(screen.queryByText(label)).not.toBeInTheDocument()
    } else {
      expect(screen.getByText(label)).toBeInTheDocument()
    }
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

  it('renders unavailable with accessible text, not an aria-hidden dash alone', () => {
    // The dash is decorative; without the adjacent text a screen reader would
    // reach this total with no accessible name at all.
    const { container } = render(<ConsolidatedMoney total={null} partialTotal={null} currency="SGD" />)
    expect(screen.getByText('Not available')).toBeInTheDocument()
    expect(container.textContent).not.toBe('—')
  })

  it('names the reasons a total is unavailable when they are known', () => {
    render(<ConsolidatedMoney
      total={null} partialTotal={null} currency="SGD"
      unavailableReasons={['RATE_NOT_IMPORTED', 'PERIOD_UNKNOWN']}
    />)
    expect(screen.getByText('RATE_NOT_IMPORTED, PERIOD_UNKNOWN')).toBeInTheDocument()
  })
})

describe('RoundingReconciliationNote', () => {
  it('renders a reconciliation note naming the delta', () => {
    render(<RoundingReconciliationNote delta={{ value: '0.01', currency: 'SGD' }} currency="SGD" />)
    expect(screen.getByText(/0.01 SGD/)).toBeInTheDocument()
  })
})
