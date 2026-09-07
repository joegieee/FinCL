import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const money = (v: string) => ({ value: v, currency: 'SGD' })

const plan = {
  plan_id: 'plan-1', card_account_id: 'mari-credit-card', currency: 'SGD',
  plan_reference: 'SPLIT-6M', merchant_label: 'Split Payment Merchant',
  opened_on: '2026-07-21', status: 'CONFIRMED', effective_status: 'CONFIRMED',
  interpretation_status: 'confirmed', bound: true, dangling_reason: null,
  progress: {
    known_principal: money('15.00'), term_months: 6,
    observed_principal_paid: money('15.00'), scheduled_principal: money('15.00'),
    settled_principal: money('0.00'), remaining_obligation: money('0.00'),
    occurrence_count: 1, occurrences_bound: 1,
  },
}

const page = { generation: 'sha256:abc', plans: [plan], next_cursor: null }

const detail: Record<string, unknown> = {
  generation: 'sha256:abc', as_of: '2026-09-01', plan,
  source_occurrences: [
    {
      occurrence_id: 'o-1', occurrence_index: 3, occurrence_count: 6,
      entry_anchor: 'sha:pdf:mari-card:aaa:5', effective_on: '2026-07-21',
      date_precision: 'DAY', amount: money('15.00'), bound: true,
    },
  ],
  purchase_allocations: [],
  schedule: [
    {
      schedule_index: 1, due_on: '2026-07-21', principal: money('2.50'),
      fee: money('0.20'), rounding_remainder: money('0.00'), is_projected: true,
    },
  ],
  settlements: [],
  conversion: null,
  suggested_conversion: null,
  views: {
    recognized_purchase_cost: {
      recognized_principal: money('15.00'), financing_fee: money('1.20'),
      note: 'Recognition components belong only to economic-cost views.',
    },
    spread_allocation: {
      observed_occurrences: [], projected_schedule: [],
      note: 'Projected schedule rows are distinct from actual observed installments.',
    },
  },
}

const candidate = {
  candidate_key: 'mari-credit-card:Split Payment:6:anchor-1',
  card_account_id: 'mari-credit-card', currency: 'SGD', family: 'Split Payment',
  term_months: 6, merchant_label: 'MERCHANT DELTA SYNTHETIC', opened_on: '2026-01-21',
  first_ordinal: 1, last_ordinal: 6, observed_count: 6, complete: true,
  observed_total: money('60.40'), known_principal: money('60.40'),
  principal_source: 'observed_chain', conversion: null,
  occurrences: [
    {
      entry_anchor: 'anchor-1', occurrence_index: 1, occurrence_count: 6,
      effective_on: '2026-01-21', date_precision: 'DAY', amount: '10.07',
      description: 'MERCHANT DELTA SYNTHETIC',
    },
  ],
  source: 'mari-card-installment-block-v1',
}

const partialCandidate = {
  ...candidate,
  candidate_key: 'partial', merchant_label: 'Split Payment · 20.30 × 12',
  term_months: 12, observed_count: 10, last_ordinal: 10, complete: false,
  observed_total: money('203.00'), known_principal: null, principal_source: null,
}

const convertedCandidate = {
  ...partialCandidate,
  candidate_key: 'converted', observed_total: money('203.00'),
  known_principal: money('243.60'), principal_source: 'conversion_credit',
  conversion: {
    entry_anchor: 'conversion-1', effective_on: '2025-11-12', amount: '243.60',
    description: 'Split Payment Conversion',
  },
}

let candidatesBody: Record<string, unknown> = {
  generation: 'sha256:abc', candidates: [candidate], ambiguous_groups: [],
  claimed_occurrence_count: 0,
}

const written: { path: string; body: unknown }[] = []

// `reporting/workspaces/installments/candidates` is matched before the `reporting/workspaces/installments/` detail branch:
// the real route is registered the same way round, and reading it as a plan id
// is exactly the regression this ordering guards.
const apiMock = vi.fn(async (path: string, body?: unknown) => {
  if (path.startsWith('reporting/workspaces/installments/candidates')) return candidatesBody
  if (path.startsWith('reporting/workspaces/installments?')) return page
  if (path.startsWith('reporting/workspaces/installments/')) return detail
  if (path === 'preview' || path === 'commands') {
    written.push({ path, body })
    return { status: 'recorded' }
  }
  throw new Error(`unmocked ${path}`)
})
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: (path: string, body?: unknown) => apiMock(path, body) }
})

async function mount(onSaved?: (status: string) => void) {
  const { InstallmentsView } = await import('./index')
  return render(<InstallmentsView generation="sha256:abc" onSaved={onSaved} />)
}

afterEach(() => {
  cleanup()
  apiMock.mockClear()
  written.length = 0
  candidatesBody = {
    generation: 'sha256:abc', candidates: [candidate], ambiguous_groups: [],
    claimed_occurrence_count: 0,
  }
})

describe('InstallmentsView', () => {
  it('lists a plan with its principal and occurrence progress', async () => {
    await mount()
    expect(await screen.findByText('Split Payment Merchant')).toBeInTheDocument()
    expect(screen.getByText(/1\/1 occurrences/)).toBeInTheDocument()
  })

  it('opens a plan and keeps the projected schedule distinct from occurrences', async () => {
    await mount()
    fireEvent.click(await screen.findByText('Split Payment Merchant'))
    await waitFor(() => expect(screen.getByText(/All plans/)).toBeInTheDocument())
    expect(screen.getByText('Source occurrences')).toBeInTheDocument()
    expect(screen.getByText('Projected schedule')).toBeInTheDocument()
    expect(screen.getByText('Recognized purchase cost')).toBeInTheDocument()
    expect(screen.getByText('Spread allocation')).toBeInTheDocument()
  })
})

describe('confirming a split payment', () => {
  it('proposes the detected chain with its progress', async () => {
    await mount()
    expect(await screen.findByText('MERCHANT DELTA SYNTHETIC')).toBeInTheDocument()
    expect(screen.getByText('Split payments to confirm')).toBeInTheDocument()
    expect(screen.getByText(/6 of 6 instalments/)).toBeInTheDocument()
    expect(screen.getByText(/fully paid/)).toBeInTheDocument()
  })

  it('prefills the principal of a fully observed chain and sends it', async () => {
    await mount()
    fireEvent.click(await screen.findByRole('button', { name: /Confirm/ }))
    const principal = await screen.findByLabelText('Known principal')
    expect(principal).toHaveValue('60.40')
    fireEvent.change(screen.getByLabelText('Reason'), {
      target: { value: 'Delta order, six-month split confirmed from the statements' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Preview and confirm/ }))
    await waitFor(() => expect(written.length).toBe(2))
    // Preview first, then the command, and both carry the same payload.
    expect(written.map((w) => w.path)).toEqual(['preview', 'commands'])
    const body = written[1].body as { kind: string; payload: Record<string, unknown> }
    expect(body.kind).toBe('INSTALLMENT_PLAN_SET')
    expect(body.payload.known_principal).toBe('60.40')
    expect(body.payload.term_months).toBe(6)
    expect(body.payload.status).toBe('CONFIRMED')
  })

  it('leaves the principal unknown on a partial chain and sends null, not a guess', async () => {
    candidatesBody = {
      generation: 'sha256:abc', candidates: [partialCandidate], ambiguous_groups: [],
      claimed_occurrence_count: 0,
    }
    await mount()
    fireEvent.click(await screen.findByRole('button', { name: /Confirm/ }))
    expect(await screen.findByLabelText('Known principal')).toHaveValue('')
    expect(screen.getByText(/2 instalments of 12 have not appeared/)).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Ongoing 12-month split' } })
    fireEvent.click(screen.getByRole('button', { name: /Preview and confirm/ }))
    await waitFor(() => expect(written.length).toBe(2))
    const body = written[1].body as { payload: Record<string, unknown> }
    expect(body.payload.known_principal).toBeNull()
    // An unknown principal cannot imply future payments, so no schedule is sent.
    expect(body.payload.schedule).toEqual([])
  })

  it('prefills a running chain from its printed conversion credit and sends it', async () => {
    candidatesBody = {
      generation: 'sha256:abc', candidates: [convertedCandidate], ambiguous_groups: [],
      claimed_occurrence_count: 0,
    }
    await mount()
    expect(await screen.findByText(/principal SGD 243\.60/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Confirm/ }))
    expect(await screen.findByLabelText('Known principal')).toHaveValue('243.60')
    expect(screen.getByText(/Split Payment Conversion credit/)).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Lego, 12-month split' } })
    fireEvent.click(screen.getByRole('button', { name: /Preview and confirm/ }))
    await waitFor(() => expect(written.length).toBe(2))
    const body = written[1].body as { payload: Record<string, unknown> }
    expect(body.payload.known_principal).toBe('243.60')
    expect(body.payload.schedule).toEqual([])
    // The plan owns the credit, so it stops asking for review.
    expect(body.payload.conversion_anchor).toBe('conversion-1')
  })

  it('sends no conversion credit for a chain without one', async () => {
    await mount()
    fireEvent.click(await screen.findByRole('button', { name: /Confirm/ }))
    fireEvent.change(await screen.findByLabelText('Reason'), { target: { value: 'Confirmed' } })
    fireEvent.click(screen.getByRole('button', { name: /Preview and confirm/ }))
    await waitFor(() => expect(written.length).toBe(2))
    const body = written[1].body as { payload: Record<string, unknown> }
    expect(body.payload.conversion_anchor).toBeNull()
  })

  it('attaches a suggested conversion credit to an existing plan, keeping the plan as it was', async () => {
    const suggestion = {
      entry_anchor: 'conversion-9', effective_on: '2026-07-12', amount: '15.00',
      description: 'Split Payment Conversion',
    }
    detail.suggested_conversion = suggestion
    try {
      await mount()
      fireEvent.click(await screen.findByText('Split Payment Merchant'))
      expect(await screen.findByText(/Split Payment Conversion credit of SGD 15\.00 on 2026-07-12/)).toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: 'Attach conversion credit' }))
      await waitFor(() => expect(written.length).toBe(2))
      const body = written[1].body as { kind: string; anchors: Record<string, string>; payload: Record<string, unknown> }
      expect(body.kind).toBe('INSTALLMENT_PLAN_SET')
      expect(body.anchors).toEqual({ plan: 'plan-1' })
      expect(body.payload).toMatchObject({
        conversion_anchor: 'conversion-9', known_principal: '15.00', financing_fee: '1.20',
        term_months: 6, plan_reference: 'SPLIT-6M', status: 'CONFIRMED',
        occurrences: [{ occurrence_id: 'o-1', entry_anchor: 'sha:pdf:mari-card:aaa:5', amount: '15.00' }],
        schedule: [{ due_on: '2026-07-21', principal: '2.50', fee: '0.20' }],
      })
    } finally {
      detail.suggested_conversion = null
    }
  })

  it('carries every detected occurrence anchor into the plan', async () => {
    await mount()
    fireEvent.click(await screen.findByRole('button', { name: /Confirm/ }))
    fireEvent.change(await screen.findByLabelText('Reason'), { target: { value: 'Confirmed' } })
    fireEvent.click(screen.getByRole('button', { name: /Preview and confirm/ }))
    await waitFor(() => expect(written.length).toBe(2))
    const body = written[1].body as { payload: { occurrences: { entry_anchor: string }[] } }
    expect(body.payload.occurrences.map((o) => o.entry_anchor)).toEqual(['anchor-1'])
  })

  it('refuses to submit without a reason', async () => {
    await mount()
    fireEvent.click(await screen.findByRole('button', { name: /Confirm/ }))
    fireEvent.click(await screen.findByRole('button', { name: /Preview and confirm/ }))
    expect(await screen.findByText(/reason are both required/)).toBeInTheDocument()
    expect(written).toHaveLength(0)
  })

  it('shows an ambiguous group as evidence and proposes nothing for it', async () => {
    candidatesBody = {
      generation: 'sha256:abc',
      candidates: [],
      ambiguous_groups: [
        {
          card_account_id: 'mari-credit-card', currency: 'SGD', family: 'Split Payment',
          term_months: 6, reason: 'Two unconfirmed chains share an instalment amount.',
          occurrences: [
            {
              entry_anchor: 'a', occurrence_index: 1, occurrence_count: 6,
              effective_on: '2026-01-21', amount: '10.00', description: null,
            },
          ],
        },
      ],
      claimed_occurrence_count: 0,
    }
    await mount()
    expect(await screen.findByText(/Two unconfirmed chains share/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Confirm/ })).not.toBeInTheDocument()
  })

  it('hides the confirm section once nothing is left to confirm', async () => {
    candidatesBody = {
      generation: 'sha256:abc', candidates: [], ambiguous_groups: [], claimed_occurrence_count: 6,
    }
    await mount()
    await screen.findByText('Installment plans')
    expect(screen.queryByText('Split payments to confirm')).not.toBeInTheDocument()
  })
})
