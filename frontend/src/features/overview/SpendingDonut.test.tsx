import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { SpendingDonut } from './SpendingDonut'
import type { Dashboard } from '@/lib/api'

afterEach(cleanup)

function totals(n: number) {
  return Array.from({ length: n }, (_, i) => ({ name: `Group / Category ${i}`, value: String(100 - i), count: i + 1 }))
}

function dashboard(overrides: Partial<Dashboard> = {}): Dashboard {
  return { category_totals: totals(3), covered: true, ...overrides } as Dashboard
}

describe('SpendingDonut', () => {
  it('renders one slice per category with no grouping at or below eight categories', () => {
    render(<SpendingDonut data={dashboard({ category_totals: totals(8) })} currency="SGD" spending="500.00" onOpenAnalysis={vi.fn()} onDrill={vi.fn()} />)
    for (let i = 0; i < 8; i++) expect(screen.getByRole('button', { name: new RegExp(`Category ${i}`) })).toBeInTheDocument()
    expect(screen.queryByText(/Other/)).not.toBeInTheDocument()
  })

  it('groups everything past the top eight into a single, named, clickable Other slice', () => {
    const onDrill = vi.fn()
    const onOpenAnalysis = vi.fn()
    render(<SpendingDonut data={dashboard({ category_totals: totals(11) })} currency="SGD" spending="500.00" onOpenAnalysis={onOpenAnalysis} onDrill={onDrill} />)
    // The eight highest-value categories (0..7) get named rows; 8, 9, 10 fold into Other.
    for (let i = 0; i < 8; i++) expect(screen.getByRole('button', { name: new RegExp(`Category ${i}(?!\\d)`) })).toBeInTheDocument()
    const other = screen.getByRole('button', { name: /Other/ })
    expect(other).toHaveTextContent('3 more categories')
    other.click()
    expect(onOpenAnalysis).toHaveBeenCalled()
    expect(onDrill).not.toHaveBeenCalled()
  })

  it('never plots NaN and never leaves a slice without a legend row', () => {
    render(<SpendingDonut data={dashboard({ category_totals: totals(20) })} currency="SGD" spending="500.00" onOpenAnalysis={vi.fn()} onDrill={vi.fn()} />)
    // 8 named rows + 1 Other row = every one of the 20 categories is accounted for.
    expect(screen.getAllByRole('button').filter(b => b.className.includes('') && b.querySelector('.dot'))).toHaveLength(9)
  })
})
