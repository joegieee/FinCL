import { afterEach, describe, expect, it, vi } from 'vitest'
import { deletePreset, loadPresets, renamePreset, resolvePreset, savePreset, type Preset } from './presets'

const ALL_APPLICABLE = { category: true, cost_behavior: true, need: true, incoming_type: true }

afterEach(() => { localStorage.clear(); vi.restoreAllMocks() })

describe('preset storage', () => {
  it('starts empty with nothing saved', () => {
    expect(loadPresets()).toEqual([])
  })

  it('saves, reloads, renames, and deletes a preset', () => {
    savePreset('Everyday groceries', { category: 'Food & Dining / Groceries', cost_behavior: 'variable_routine', need: 'essential' })
    let all = loadPresets()
    expect(all).toHaveLength(1)
    expect(all[0].name).toBe('Everyday groceries')
    expect(all[0].fields).toEqual({ category: 'Food & Dining / Groceries', cost_behavior: 'variable_routine', need: 'essential' })

    all = renamePreset(all[0].id, 'Weekly groceries')
    expect(all[0].name).toBe('Weekly groceries')
    expect(loadPresets()[0].name).toBe('Weekly groceries')

    all = deletePreset(all[0].id)
    expect(all).toEqual([])
    expect(loadPresets()).toEqual([])
  })

  it('trims whitespace from names on save and rename', () => {
    savePreset('  Groceries  ', { category: 'Food & Dining / Groceries' })
    const [preset] = loadPresets()
    expect(preset.name).toBe('Groceries')
    renamePreset(preset.id, '  Renamed  ')
    expect(loadPresets()[0].name).toBe('Renamed')
  })

  it('ignores malformed JSON in storage instead of throwing', () => {
    localStorage.setItem('fincl.classification.presets.v1', '{not json')
    expect(loadPresets()).toEqual([])
  })

  it('ignores a wrong-version or non-array payload', () => {
    localStorage.setItem('fincl.classification.presets.v1', JSON.stringify({ version: 2, presets: [{ id: 'a', name: 'x', fields: {} }] }))
    expect(loadPresets()).toEqual([])
    localStorage.setItem('fincl.classification.presets.v1', JSON.stringify({ version: 1, presets: 'nope' }))
    expect(loadPresets()).toEqual([])
  })

  it('drops individually malformed presets while keeping valid ones', () => {
    localStorage.setItem('fincl.classification.presets.v1', JSON.stringify({
      version: 1,
      presets: [
        { id: 'ok', name: 'Valid', fields: { category: 'Food & Dining / Groceries' } },
        { id: 'bad-fields', name: 'Bad', fields: { category: 123 } },
        { id: 'bad-name', fields: {} },
        'not-an-object',
      ],
    }))
    const all = loadPresets()
    expect(all).toHaveLength(1)
    expect(all[0].id).toBe('ok')
  })

  it('degrades gracefully when localStorage is unavailable', () => {
    const getSpy = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('disabled') })
    const setSpy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('disabled') })
    expect(loadPresets()).toEqual([])
    expect(() => savePreset('X', { category: 'Food & Dining / Groceries' })).not.toThrow()
    getSpy.mockRestore(); setSpy.mockRestore()
  })
})

describe('resolvePreset', () => {
  function preset(fields: Preset['fields']): Preset { return { id: 'p1', name: 'Test', fields } }

  it('passes through every applicable, valid field', () => {
    const result = resolvePreset(
      preset({ category: 'Food & Dining / Groceries', cost_behavior: 'variable_routine', need: 'essential' }),
      ALL_APPLICABLE, ['Food & Dining / Groceries'],
    )
    expect(result).toEqual({ fields: { category: 'Food & Dining / Groceries', cost_behavior: 'variable_routine', need: 'essential' }, staleCategory: false })
  })

  it('drops fields that are not applicable in the current context without flagging them stale', () => {
    const result = resolvePreset(
      preset({ category: 'Food & Dining / Groceries', incoming_type: 'income' }),
      { category: true, cost_behavior: false, need: false, incoming_type: false },
      ['Food & Dining / Groceries'],
    )
    expect(result.fields).toEqual({ category: 'Food & Dining / Groceries' })
    expect(result.staleCategory).toBe(false)
  })

  it('flags an archived/deleted category as stale instead of silently remapping it', () => {
    const result = resolvePreset(preset({ category: 'Food & Dining / Groceries (archived)' }), ALL_APPLICABLE, ['Food & Dining / Groceries'])
    expect(result.fields.category).toBeUndefined()
    expect(result.staleCategory).toBe(true)
  })

  it('never applies a category that is only inapplicable, even if canonical', () => {
    const result = resolvePreset(
      preset({ category: 'Food & Dining / Groceries' }),
      { category: false, cost_behavior: true, need: true, incoming_type: true },
      ['Food & Dining / Groceries'],
    )
    expect(result.fields.category).toBeUndefined()
    expect(result.staleCategory).toBe(false)
  })

  it('drops an unrecognized enum value from corrupted storage', () => {
    const result = resolvePreset(preset({ cost_behavior: 'not_a_real_value' }), ALL_APPLICABLE, [])
    expect(result.fields.cost_behavior).toBeUndefined()
  })
})
