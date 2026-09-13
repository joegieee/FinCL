import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: () => Promise.resolve({ categories: [] }) }
})

const { PresetPicker } = await import('./PresetPicker')
const { savePreset } = await import('./presets')

const ALL_APPLICABLE = { category: true, cost_behavior: true, need: true, incoming_type: true }
const CATEGORIES = ['Food & Dining / Groceries']

afterEach(() => { cleanup(); localStorage.clear(); vi.restoreAllMocks() })

describe('PresetPicker', () => {
  it('cannot save with no field chosen, and saves once a field is chosen', () => {
    const onApply = vi.fn()
    const { rerender } = render(<PresetPicker applicable={ALL_APPLICABLE} categories={CATEGORIES} current={{}} onApply={onApply}/>)
    expect(screen.getByText('Save as preset').closest('button')).toBeDisabled()

    rerender(<PresetPicker applicable={ALL_APPLICABLE} categories={CATEGORIES}
      current={{ category: 'Food & Dining / Groceries', cost_behavior: 'variable_routine', need: 'essential' }} onApply={onApply}/>)
    fireEvent.change(screen.getByLabelText('New preset name'), { target: { value: 'Everyday groceries' } })
    fireEvent.click(screen.getByText('Save as preset'))
    expect(screen.getByRole('button', { name: 'Everyday groceries' })).toBeInTheDocument()
    expect(screen.getByText(/Groceries.*Variable routine.*Essential/)).toBeInTheDocument()
  })

  it('applies only the fields the preset has, handing them to onApply verbatim', () => {
    savePreset('Everyday groceries', { category: 'Food & Dining / Groceries', cost_behavior: 'variable_routine', need: 'essential' })
    const onApply = vi.fn()
    render(<PresetPicker applicable={ALL_APPLICABLE} categories={CATEGORIES} current={{}} onApply={onApply}/>)
    fireEvent.click(screen.getByRole('button', { name: 'Everyday groceries' }))
    expect(onApply).toHaveBeenCalledWith({ category: 'Food & Dining / Groceries', cost_behavior: 'variable_routine', need: 'essential' })
  })

  it('drops fields not applicable in this context and never calls onApply for an empty result', () => {
    savePreset('Incoming gift', { incoming_type: 'gift' })
    const onApply = vi.fn()
    render(<PresetPicker applicable={{ category: true, cost_behavior: true, need: true, incoming_type: false }} categories={CATEGORIES} current={{}} onApply={onApply}/>)
    fireEvent.click(screen.getByRole('button', { name: 'Incoming gift' }))
    expect(onApply).not.toHaveBeenCalled()
  })

  it('surfaces a stale category instead of silently applying or remapping it', () => {
    savePreset('Old preset', { category: 'Deleted / Category', cost_behavior: 'variable_routine' })
    const onApply = vi.fn()
    render(<PresetPicker applicable={ALL_APPLICABLE} categories={CATEGORIES} current={{}} onApply={onApply}/>)
    fireEvent.click(screen.getByRole('button', { name: 'Old preset' }))
    expect(onApply).toHaveBeenCalledWith({ cost_behavior: 'variable_routine' })
    expect(screen.getByRole('status')).toHaveTextContent(/archived or no longer exists/)
  })

  it('renames a preset in place', () => {
    savePreset('Old name', { category: 'Food & Dining / Groceries' })
    render(<PresetPicker applicable={ALL_APPLICABLE} categories={CATEGORIES} current={{}} onApply={vi.fn()}/>)
    fireEvent.click(screen.getByLabelText('Rename Old name'))
    fireEvent.change(screen.getByLabelText('Rename Old name'), { target: { value: 'New name' } })
    fireEvent.click(screen.getByText('Save name'))
    expect(screen.getByRole('button', { name: 'New name' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Old name' })).not.toBeInTheDocument()
  })

  it('deletes a preset only after confirmation', () => {
    savePreset('Doomed', { category: 'Food & Dining / Groceries' })
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    render(<PresetPicker applicable={ALL_APPLICABLE} categories={CATEGORIES} current={{}} onApply={vi.fn()}/>)
    fireEvent.click(screen.getByLabelText('Delete Doomed'))
    expect(screen.getByRole('button', { name: 'Doomed' })).toBeInTheDocument()

    vi.spyOn(window, 'confirm').mockReturnValue(true)
    fireEvent.click(screen.getByLabelText('Delete Doomed'))
    expect(screen.queryByRole('button', { name: 'Doomed' })).not.toBeInTheDocument()
  })
})
