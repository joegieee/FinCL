import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { applyTheme, nextTheme, readTheme } from './theme'

beforeEach(() => {
  localStorage.clear()
  document.documentElement.removeAttribute('data-theme')
})
afterEach(() => { vi.restoreAllMocks() })

describe('theme', () => {
  it('follows the system until something is chosen', () => {
    expect(readTheme()).toBe('system')
  })

  it('writes no attribute for system, so the media query stays in charge', () => {
    applyTheme('dark')
    applyTheme('system')

    expect(document.documentElement.hasAttribute('data-theme')).toBe(false)
  })

  it('marks an explicit choice on the document and remembers it', () => {
    applyTheme('dark')

    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
    expect(readTheme()).toBe('dark')
  })

  it('cycles system to light to dark and back', () => {
    expect(nextTheme('system')).toBe('light')
    expect(nextTheme('light')).toBe('dark')
    expect(nextTheme('dark')).toBe('system')
  })

  it('still applies the theme when storage is unavailable', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked') })

    expect(() => applyTheme('dark')).not.toThrow()
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
  })

  it('falls back to system when storage cannot be read', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked') })

    expect(readTheme()).toBe('system')
  })

  it('ignores a stored value that is not a theme', () => {
    localStorage.setItem('fincl.theme', 'chartreuse')

    expect(readTheme()).toBe('system')
  })
})
