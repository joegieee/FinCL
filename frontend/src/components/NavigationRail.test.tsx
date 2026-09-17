import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { NavigationRail } from './NavigationRail'

function renderRail(over: Partial<Parameters<typeof NavigationRail>[0]> = {}) {
  const onSelect = vi.fn()
  render(
    <NavigationRail
      tab="Overview" onSelect={onSelect} reviewCount={0} proposalCount={0}
      collapsed={false} onToggleCollapse={() => {}} ledgerLabel="Personal ledger"
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

  it('opens a group and its destinations, and navigates to its lead', () => {
    const onSelect = renderRail()

    fireEvent.click(screen.getByRole('button', { name: /^Commitments/ }))

    expect(onSelect).toHaveBeenCalledWith('Obligations')
    expect(screen.getByRole('button', { name: /^Installments/ })).toBeInTheDocument()
  })

  it('keeps the active destination’s group open without being asked', () => {
    renderRail({ tab: 'Installments' })

    expect(screen.getByRole('button', { name: /^Installments/ })).toHaveAttribute('aria-current', 'page')
  })

  it('leaves a browsed group open when another is opened', () => {
    renderRail()

    fireEvent.click(screen.getByRole('button', { name: /^Commitments/ }))
    fireEvent.click(screen.getByRole('button', { name: /^Money$/ }))

    expect(screen.getByRole('button', { name: /^Installments/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Transactions/ })).toBeInTheDocument()
  })

  it('treats a single-destination group as its own row, not an expander', () => {
    const onSelect = renderRail({ tab: 'Transactions' })

    const overview = screen.getByRole('button', { name: /^Overview/ })
    expect(overview).not.toHaveAttribute('aria-expanded')
    fireEvent.click(overview)
    expect(onSelect).toHaveBeenCalledWith('Overview')
  })

  it('rolls a closed group’s pending counts up onto the group row', () => {
    renderRail({ reviewCount: 4, proposalCount: 2 })

    const money = screen.getByRole('button', { name: /^Money/ })
    expect(money).toHaveTextContent('6')
  })

  it('only expands a group in the mobile drawer, since navigating closes it', () => {
    const onSelect = renderRail({ drawer: true })

    fireEvent.click(screen.getByRole('button', { name: /^Commitments/ }))

    expect(onSelect).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: /^Installments/ })).toBeInTheDocument()
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
