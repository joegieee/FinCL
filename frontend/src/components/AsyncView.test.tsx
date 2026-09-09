import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { AsyncView } from './AsyncView'

describe('AsyncView', () => {
  it('shows a skeleton while loading with no data yet', () => {
    render(<AsyncView loading error="" data={null} onRetry={() => {}} loadingLabel="Loading widgets">{() => <p>content</p>}</AsyncView>)
    expect(screen.getByLabelText('Loading widgets')).toBeInTheDocument()
    expect(screen.queryByText('content')).not.toBeInTheDocument()
  })

  it('shows a retry prompt on error with no data yet', () => {
    const onRetry = vi.fn()
    render(<AsyncView loading={false} error="boom" data={null} onRetry={onRetry} errorTitle="Could not load widgets">{() => <p>content</p>}</AsyncView>)
    expect(screen.getByText('Could not load widgets')).toBeInTheDocument()
    expect(screen.getByText('boom')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /try again/i }))
    expect(onRetry).toHaveBeenCalled()
  })

  it('renders children with the narrowed data once ready', () => {
    render(<AsyncView loading={false} error="" data={{ label: 'Widget A' }} onRetry={() => {}}>{data => <p>{data.label}</p>}</AsyncView>)
    expect(screen.getByText('Widget A')).toBeInTheDocument()
  })

  it('keeps showing stale data during a refresh instead of blanking to the skeleton', () => {
    render(<AsyncView loading error="" data={{ label: 'Widget A' }} onRetry={() => {}}>{data => <p>{data.label}</p>}</AsyncView>)
    expect(screen.getByText('Widget A')).toBeInTheDocument()
  })
})
