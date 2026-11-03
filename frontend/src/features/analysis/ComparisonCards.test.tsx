import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import type { ReportedAnalysis } from './reportedApi'
import { ComparisonCards } from './ComparisonCards'

const entry = (delta: string) => ({
  current: { total: { value: '100.00', currency: 'SGD' }, partial_total: null, unavailable_reasons: [], excluded_detail: null },
  previous: { total: { value: '90.00', currency: 'SGD' } },
  comparison_available: true, comparison_reasons: [], delta: { value: delta, currency: 'SGD' }, pct: '10.0',
})

const analysis = (key: string, delta: string) => ({ reporting_currency: 'SGD', comparison_kind: 'previous_month', summary: { [key]: entry(delta) } }) as unknown as ReportedAnalysis

function chip() {
  return screen.getByText('10.0%').closest('.delta')
}

describe('ComparisonCards colours', () => {
  afterEach(cleanup)

  it('reads more spending as worse', () => {
    render(<ComparisonCards analysis={analysis('spending', '10.00')} />)
    expect(chip()).toHaveClass('up')
  })

  it.each(['refunds', 'net_economic_flow'])('reads a rise in %s as better', key => {
    render(<ComparisonCards analysis={analysis(key, '10.00')} />)
    expect(chip()).toHaveClass('down')
  })

  it('reads a fall in net flow as worse', () => {
    render(<ComparisonCards analysis={analysis('net_economic_flow', '-10.00')} />)
    expect(chip()).toHaveClass('up')
  })
})
