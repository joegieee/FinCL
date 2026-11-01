import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { NavigationRail, SectionBar, SectionTabs } from './NavigationRail'

function renderRail(over: Partial<Parameters<typeof NavigationRail>[0]> = {}) {
  const onSelect = vi.fn()
  render(
    <NavigationRail
      tab="Overview" onSelect={onSelect} reviewCount={0} proposalCount={0}
      ledgerLabel="Personal ledger" onSearch={() => {}}
      onHistory={() => {}} historyDisabled={false} onRefresh={() => {}}
      refreshDisabled={false} refreshing={false} demo={false}
      {...over}
    />,
  )
  return onSelect
}

beforeEach(() => { localStorage.clear(); document.documentElement.removeAttribute('data-theme') })
afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('NavigationRail grouping', () => {
  it('shows six groups rather than every destination at once', () => {
    renderRail()

    const nav = screen.getByRole('navigation', { name: 'Main navigation' })
    const groups = within(nav).getAllByRole('button').filter(b => b.className.includes('rail-group'))
    expect(groups).toHaveLength(6)
    expect(screen.queryByRole('button', { name: /^Installments/ })).toBeNull()
  })

  it('navigates to a section\u2019s lead from its row', () => {
    const onSelect = renderRail()

    fireEvent.click(screen.getByRole('button', { name: /^Commitments/ }))

    expect(onSelect).toHaveBeenCalledWith('Obligations')
  })

  it('opens only the section holding the current page', () => {
    renderRail({ tab: 'Installments' })

    expect(screen.getByRole('button', { name: /^Installments/ })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('button', { name: /^Commitments/ })).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByRole('button', { name: /^Money/ })).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('button', { name: /^Transactions/ })).toBeNull()
  })

  it('opens the command palette from the search row', () => {
    const onSearch = vi.fn()
    renderRail({ onSearch })

    fireEvent.click(screen.getByRole('button', { name: /^Go anywhere/ }))

    expect(onSearch).toHaveBeenCalled()
  })

  it('treats a single-destination group as its own row, not an expander', () => {
    const onSelect = renderRail({ tab: 'Transactions' })

    const overview = screen.getByRole('button', { name: /^Overview/ })
    expect(overview).not.toHaveAttribute('aria-expanded')
    fireEvent.click(overview)
    expect(onSelect).toHaveBeenCalledWith('Overview')
  })

  it('marks a closed section when something inside it is waiting, and counts it on the page once open', () => {
    renderRail({ reviewCount: 4, proposalCount: 2 })
    expect(screen.getByLabelText('6 waiting')).toBeInTheDocument()

    cleanup()
    renderRail({ tab: 'Review', reviewCount: 4, proposalCount: 2 })
    expect(screen.queryByLabelText('6 waiting')).toBeNull()
    expect(screen.getByRole('button', { name: /^Review/ })).toHaveTextContent('2')
  })

  it('walks only the destinations on screen with the arrow keys', () => {
    renderRail({ tab: 'Obligations' })

    const nav = screen.getByRole('navigation', { name: 'Main navigation' })
    const rows = Array.from(nav.querySelectorAll('ul button')) as HTMLButtonElement[]
    rows[0].focus()
    fireEvent.keyDown(rows[0], { key: 'End' })

    expect(document.activeElement).toBe(rows[rows.length - 1])
  })
})

describe('Small-screen navigation', () => {
  it('lists the six sections and goes to the lead of the one chosen', () => {
    const onSelect = vi.fn()
    render(<SectionBar tab="Debts" onSelect={onSelect} reviewCount={0} proposalCount={0} />)

    const buttons = within(screen.getByRole('navigation', { name: 'Sections' })).getAllByRole('button')
    expect(buttons).toHaveLength(6)
    expect(screen.getByRole('button', { name: 'Commitments' })).toHaveAttribute('aria-current', 'true')
    fireEvent.click(screen.getByRole('button', { name: 'Money' }))
    expect(onSelect).toHaveBeenCalledWith('Transactions')
  })

  it('lists the current section\u2019s pages, and nothing for a section of one', () => {
    const onSelect = vi.fn()
    const { unmount } = render(<SectionTabs tab="Debts" onSelect={onSelect} reviewCount={0} proposalCount={0} />)

    expect(screen.getByRole('button', { name: 'Debts' })).toHaveAttribute('aria-current', 'page')
    fireEvent.click(screen.getByRole('button', { name: 'Cards' }))
    expect(onSelect).toHaveBeenCalledWith('Cards')
    unmount()

    render(<SectionTabs tab="Overview" onSelect={onSelect} reviewCount={0} proposalCount={0} />)
    expect(screen.queryByRole('navigation')).toBeNull()
  })
})

describe('NavigationRail theme control', () => {
  it('cycles the theme and records the explicit choice on the document', () => {
    renderRail()

    fireEvent.click(screen.getByRole('button', { name: 'Theme: system' }))
    expect(document.documentElement.getAttribute('data-theme')).toBe('light')

    fireEvent.click(screen.getByRole('button', { name: 'Theme: light' }))
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')

    fireEvent.click(screen.getByRole('button', { name: 'Theme: dark' }))
    expect(document.documentElement.hasAttribute('data-theme')).toBe(false)
  })

  it('restores the remembered theme on the next visit', () => {
    localStorage.setItem('fincl.theme', 'dark')

    renderRail()

    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
  })
})
