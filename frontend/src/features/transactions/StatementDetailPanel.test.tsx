import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { StatementDetailPanel } from './StatementDetailPanel'
import type { TransactionRowOut } from './reportedApi'

function row(over: Partial<TransactionRowOut['statement_detail']> = {}, amountBasis: string | null = 'identity'): TransactionRowOut {
  return {
    entry: { id: 'e1', anchor: 'a1', date: '2026-02-01', precision: 'DAY', amount: '-100.00', fee: '0', currency: 'SGD', account: 'Revolut', merchant: 'Foo', description: 'FOO', category: 'Food / Dining', basis: 'rule', source_location: 'p1', bucket: 'spending', eligible: true, review: false, source_family: 'REVOLUT_PDF', lifecycle_state: 'COMPLETED', cost_behavior: 'irregular', cost_behavior_basis: 'manual', need: 'discretionary', need_basis: 'manual', role_review_state: 'none', suggested_classification: '' },
    amount: { native: { value: '-100.00', currency: 'SGD' }, reported: amountBasis === 'identity' ? null : { value: '-100.00', currency: 'SGD' }, purpose: 'FLOW', basis: amountBasis as never, valuation_as_of: '2026-02-01', rate_as_of: null, rate_period: null, rate_path: [], forward_policy: null, unavailable: null },
    fee: null,
    statement_detail: {
      original: null,
      settled: { currency: 'SGD', amount_text: '100.00' },
      explicit_fee: null,
      statement_cash_movement: null,
      printed_conversion: null,
      printed_charge_percent: null,
      effective_rate: null,
      validation: { status: 'not_checkable', reason: null },
      provenance: { document_record_id: 'doc1', source_location: 'p1', detail_basis: 'stored_columns', normalizer_version: 'v1' },
      ...over,
    },
  }
}

afterEach(cleanup)

describe('StatementDetailPanel', () => {
  it('renders a recovered INR original and the full printed provider/benchmark line verbatim, with no separate structured benchmark figure', () => {
    render(<StatementDetailPanel row={row({
      original: { currency: 'INR', amount_text: '6000.00' },
      printed_conversion: { base_currency: 'SGD', quote_currency: 'INR', base_amount_text: '1.00', quote_amount_text: '60.00', raw_text: 'Revolut Rate ₹1.00 = 60.00 INR (ECB rate* ₹1.00 = 59.50 INR) 6000.00 INR' },
      provenance: { document_record_id: 'doc1', source_location: 'p1', detail_basis: 'decoded_statement_text', normalizer_version: 'v1' },
    })} />)
    expect(screen.getByText('6000.00 INR')).toBeInTheDocument()
    // The full printed line (provider rate + parenthesized ECB benchmark)
    // survives verbatim in raw_text — the DTO has no separate benchmark
    // field, so the benchmark is never given its own structured display.
    expect(screen.getByText(/Revolut Rate.*59\.50.*6000\.00 INR/)).toBeInTheDocument()
  })

  it('renders an integer-grouped VND original as recovered evidence', () => {
    render(<StatementDetailPanel row={row({ original: { currency: 'VND', amount_text: '500000' } })} />)
    expect(screen.getByText('500000 VND')).toBeInTheDocument()
  })

  it('labels a derived effective rate as derived, never as a printed statement fact', () => {
    render(<StatementDetailPanel row={row({
      original: { currency: 'USD', amount_text: '10.00' },
      effective_rate: { base_currency: 'USD', quote_currency: 'SGD', numerator: '27', denominator: '20', amount_basis: 'fees_excluded' },
    })} />)
    expect(screen.getByText(/Derived from settled\/original amounts — not a printed statement fact\./)).toBeInTheDocument()
  })

  it('presents a rate mismatch as a note about the statement detail, never as invalidating the native entry', () => {
    render(<StatementDetailPanel row={row({ validation: { status: 'mismatch', reason: 'printed rate off by more than rounding' } })} />)
    expect(screen.getByText(/does not call the recorded native amount into question/)).toBeInTheDocument()
  })

  it('keeps a conflicting-fields note without claiming the stored value was replaced', () => {
    render(<StatementDetailPanel row={row({ validation: { status: 'conflicting_fields', reason: null } })} />)
    expect(screen.getByText(/stored value is shown and kept authoritative/)).toBeInTheDocument()
  })
})
