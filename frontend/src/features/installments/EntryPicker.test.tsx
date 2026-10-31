import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const money = (v: string) => ({ value: v, currency: 'SGD' })

const fullyOpen = {
  entry_anchor: 'anchor-purchase', effective_at: '2026-01-15', account_id: 'mari-credit-card',
  currency: 'SGD', amount: '175.00', open_amount: '175.00', open_fee: '0.00',
  description: 'MERCHANT DELTA SYNTHETIC',
}

const partlyClaimed = {
  entry_anchor: 'anchor-partial', effective_at: '2026-01-10', account_id: 'mari-credit-card',
  currency: 'SGD', amount: '90.00', open_amount: '30.00', open_fee: '0.00',
  description: 'PARTLY CLAIMED SHOP',
}

const plan = {
  plan_id: 'plan-1', card_account_id: 'mari-credit-card', currency: 'SGD',
  plan_reference: null, merchant_label: 'Delta Synthetic', opened_on: '2026-01-21',
  status: 'CONFIRMED', effective_status: 'CONFIRMED', interpretation_status: 'confirmed',
  bound: true, dangling_reason: null,
  progress: {
    known_principal: money('175.00'), term_months: 3,
    observed_principal_paid: money('175.00'), scheduled_principal: money('175.00'),
    settled_principal: money('0.00'), remaining_obligation: money('0.00'),
    occurrence_count: 3, occurrences_bound: 3,
  },
}

const detail = {
  generation: 'sha256:abc', as_of: '2026-09-01', plan,
  source_occurrences: [], purchase_allocations: [], schedule: [], settlements: [],
  views: {
    recognized_purchase_cost: { recognized_principal: null, financing_fee: null, note: '' },
    spread_allocation: { observed_occurrences: [], projected_schedule: [], note: '' },
  },
}

let candidateRows = [fullyOpen, partlyClaimed]
const searches: string[] = []
const written: { path: string; body: unknown }[] = []
let refusal: Error | null = null

const apiMock = vi.fn(async (path: string, body?: unknown) => {
  if (path.startsWith('reporting/workspaces/installments/entry-candidates')) {
    const term = new URLSearchParams(path.split('?')[1]).get('search')
    searches.push(term ?? '')
    const rows = term
      ? candidateRows.filter((r) => r.description.toLowerCase().includes(term.toLowerCase()))
      : candidateRows
    return {
      generation: 'sha256:abc', plan_id: 'plan-1',
      purpose: new URLSearchParams(path.split('?')[1]).get('purpose'),
      currency: 'SGD', card_account_id: 'mari-credit-card',
      candidates: rows, next_cursor: null,
    }
  }
  if (path === 'preview' || path === 'commands') {
    written.push({ path, body })
    if (path === 'preview' && refusal) throw refusal
    return { status: 'recorded' }
  }
  throw new Error(`unmocked ${path}`)
})
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: (path: string, body?: unknown) => apiMock(path, body) }
})

afterEach(() => {
  cleanup()
  apiMock.mockClear()
  written.length = 0
  searches.length = 0
  refusal = null
  candidateRows = [fullyOpen, partlyClaimed]
})

async function settle() {
  await waitFor(() =>
    expect(screen.queryByText('Loading transactions…')).not.toBeInTheDocument(),
  )
}

async function mountAttribution() {
  const { PurchaseAttributionForm } = await import('./PurchaseAttributionForm')
  const result = render(
    <PurchaseAttributionForm
      detail={detail}
      generation="sha256:abc"
      onSaved={() => {}}
      onCancel={() => {}}
    />,
  )
  await settle()
  return result
}

async function mountSettlement() {
  const { SettlementForm } = await import('./SettlementForm')
  const result = render(
    <SettlementForm
      detail={detail}
      generation="sha256:abc"
      onSaved={() => {}}
      onCancel={() => {}}
    />,
  )
  await settle()
  return result
}

describe('EntryPicker', () => {
  it('shows the unclaimed amount beside the full amount', async () => {
    await mountAttribution()
    // The partly claimed row must not read as if all 90.00 were available.
    expect(screen.getByText('PARTLY CLAIMED SHOP')).toBeInTheDocument()
    expect(screen.getByText(/partly claimed\)/)).toBeInTheDocument()
  })

  it('searches on Enter rather than on every keystroke', async () => {
    await mountAttribution()
    await screen.findByText('MERCHANT DELTA SYNTHETIC')
    const box = screen.getByLabelText('Search transactions')
    fireEvent.change(box, { target: { value: 'DELTA' } })
    expect(searches).toEqual([''])
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => expect(searches).toEqual(['', 'DELTA']))
  })

  it('reports an empty result instead of an empty table', async () => {
    candidateRows = []
    await mountAttribution()
    expect(await screen.findByText(/No unclaimed charges on this card match/)).toBeInTheDocument()
  })
})

describe('purchase attribution', () => {
  it('defaults a picked line to the whole unclaimed amount as principal', async () => {
    await mountAttribution()
    fireEvent.click(await screen.findByLabelText('Use MERCHANT DELTA SYNTHETIC'))
    expect(await screen.findByLabelText('Amount for MERCHANT DELTA SYNTHETIC')).toHaveValue('175.00')
    expect(screen.getByLabelText('Component for MERCHANT DELTA SYNTHETIC')).toHaveValue('principal')
  })

  it('sends the allocation the operator assembled', async () => {
    await mountAttribution()
    fireEvent.click(await screen.findByLabelText('Use MERCHANT DELTA SYNTHETIC'))
    fireEvent.change(await screen.findByLabelText('Reason'), {
      target: { value: 'The Delta order this plan splits' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Preview and save/ }))
    await waitFor(() => expect(written.length).toBe(2))
    expect(written.map((w) => w.path)).toEqual(['preview', 'commands'])
    const body = written[1].body as { kind: string; payload: { allocations: unknown[] } }
    expect(body.kind).toBe('INSTALLMENT_ALLOCATION_SET')
    expect(body.payload.allocations).toEqual([
      { entry_anchor: 'anchor-purchase', component_role: 'principal', amount: '175.00' },
    ])
  })

  it('shows the refusal of an amount beyond what is unclaimed and saves nothing', async () => {
    const { ApiError } = await import('@/lib/api')
    refusal = new ApiError('PARTLY CLAIMED SHOP has only 30.00 unclaimed in its principal component', 422)
    await mountAttribution()
    fireEvent.click(await screen.findByLabelText('Use PARTLY CLAIMED SHOP'))
    fireEvent.change(await screen.findByLabelText('Amount for PARTLY CLAIMED SHOP'), {
      target: { value: '90.00' },
    })
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Too much' } })
    fireEvent.click(screen.getByRole('button', { name: /Preview and save/ }))
    expect(await screen.findByText(/only 30.00 unclaimed/)).toBeInTheDocument()
    expect(written.map((w) => w.path)).toEqual(['preview'])
  })

  it('does not add the same transaction twice', async () => {
    await mountAttribution()
    const radio = await screen.findByLabelText('Use MERCHANT DELTA SYNTHETIC')
    fireEvent.click(radio)
    fireEvent.click(radio)
    expect(await screen.findAllByLabelText('Amount for MERCHANT DELTA SYNTHETIC')).toHaveLength(1)
  })

  it('requires a source and a reason', async () => {
    await mountAttribution()
    fireEvent.click(screen.getByRole('button', { name: /Preview and save/ }))
    expect(await screen.findByText(/Choose at least one source transaction/)).toBeInTheDocument()
    expect(written).toHaveLength(0)
  })
})

describe('settlement', () => {
  it('seeds the date and principal from the picked transaction', async () => {
    await mountSettlement()
    fireEvent.click(await screen.findByLabelText('Use MERCHANT DELTA SYNTHETIC'))
    await waitFor(() => expect(screen.getByLabelText('Settled on')).toHaveValue('2026-01-15'))
    expect(screen.getByLabelText('Settlement principal')).toHaveValue('175.00')
  })

  it('sends the picked anchor rather than anything typed', async () => {
    await mountSettlement()
    fireEvent.click(await screen.findByLabelText('Use MERCHANT DELTA SYNTHETIC'))
    fireEvent.change(await screen.findByLabelText('Reason'), {
      target: { value: 'Paid the balance off in one transfer' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Preview and save/ }))
    await waitFor(() => expect(written.length).toBe(2))
    const body = written[1].body as { kind: string; payload: Record<string, unknown> }
    expect(body.kind).toBe('INSTALLMENT_SETTLEMENT_SET')
    expect(body.payload.source_entry_anchor).toBe('anchor-purchase')
    expect(body.payload.principal_amount).toBe('175.00')
  })

  it('shows the refusal of a settlement with nothing in it and saves nothing', async () => {
    const { ApiError } = await import('@/lib/api')
    refusal = new ApiError('a settlement needs a positive principal, fee or waiver component', 422)
    await mountSettlement()
    fireEvent.click(await screen.findByLabelText('Use MERCHANT DELTA SYNTHETIC'))
    fireEvent.change(await screen.findByLabelText('Settlement principal'), { target: { value: '0' } })
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Nothing paid' } })
    fireEvent.click(screen.getByRole('button', { name: /Preview and save/ }))
    expect(await screen.findByText(/needs a positive principal, fee or waiver/)).toBeInTheDocument()
    expect(written.map((w) => w.path)).toEqual(['preview'])
  })

  it('will not submit without a chosen transaction', async () => {
    await mountSettlement()
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'No source picked' } })
    fireEvent.click(screen.getByRole('button', { name: /Preview and save/ }))
    expect(await screen.findByText(/Choose the settling transaction/)).toBeInTheDocument()
    expect(written).toHaveLength(0)
  })
})
