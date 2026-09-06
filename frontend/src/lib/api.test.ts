import { describe, expect, it } from 'vitest'
import { compareMoney, money, shortCategory } from './api'

describe('money', () => {
  it('renders an unavailable amount as text, never as zero', () => {
    expect(money(null)).toBe('Unavailable')
    expect(money(undefined)).toBe('Unavailable')
  })

  it('formats with thousands separators and two decimal places', () => {
    expect(money('1234.5', 'SGD')).toBe('SGD 1,234.50')
    expect(money('0', 'USD')).toBe('USD 0.00')
  })

  it('keeps every digit of values beyond the JS safe-integer range', () => {
    expect(money('900719925474099123.45')).toBe('SGD 900,719,925,474,099,123.45')
  })

  it('preserves the sign of a negative amount', () => {
    expect(money('-42.00', 'SGD')).toBe('−SGD 42.00')
  })
})

describe('compareMoney', () => {
  it('orders across differing scales without lossy rescaling', () => {
    expect(compareMoney('10.5', '10.50')).toBe(0)
    expect(compareMoney('2.9', '2.85')).toBe(1)
    expect(compareMoney('2.85', '2.9')).toBe(-1)
  })

  it('compares large values exactly', () => {
    expect(compareMoney('900719925474099123.45', '900719925474099123.46')).toBe(-1)
  })
})

describe('shortCategory', () => {
  it('returns the leaf of a Group / Category string', () => {
    expect(shortCategory('Food & Dining / Cafes & Drinks')).toBe('Cafes & Drinks')
    expect(shortCategory('Unclassified')).toBe('Unclassified')
  })
})
