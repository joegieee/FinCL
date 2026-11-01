import { describe, expect, it } from 'vitest'
import { periodLabel } from './format'

describe('periodLabel', () => {
  it('names a whole month', () => {
    expect(periodLabel('2026-08-01', '2026-08-31')).toBe('August 2026')
    expect(periodLabel('2024-02-01', '2024-02-29')).toBe('February 2024')
  })

  it('gives both dates for anything else', () => {
    expect(periodLabel('2026-08-01', '2026-08-30')).toMatch(/30.*Aug.*2026|Aug.*30.*2026/)
    expect(periodLabel('2026-06-01', '2026-08-31')).toContain('–')
  })
})
