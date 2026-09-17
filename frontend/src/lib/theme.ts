/**
 * Light, dark, or whatever the system says.
 *
 * The choice is written to `data-theme` on the document element and mirrored to
 * `localStorage`. `system` writes no attribute at all, which is what lets the
 * `prefers-color-scheme` rules in `index.css` take over — so "follow the system"
 * really does follow it, including when it changes while the app is open,
 * rather than snapshotting it once at load.
 *
 * Storage can throw (a private window, blocked site data), so every read and
 * write is guarded and the app renders correctly without it.
 */
export type Theme = 'system' | 'light' | 'dark'

const KEY = 'fincl.theme'
const ORDER: Theme[] = ['system', 'light', 'dark']

export function readTheme(): Theme {
  try {
    const stored = localStorage.getItem(KEY)
    if (stored === 'light' || stored === 'dark' || stored === 'system') return stored
  } catch { /* unavailable storage falls through to the system default */ }
  return 'system'
}

export function applyTheme(theme: Theme): void {
  const root = typeof document === 'undefined' ? null : document.documentElement
  if (root) {
    if (theme === 'system') root.removeAttribute('data-theme')
    else root.setAttribute('data-theme', theme)
  }
  try {
    localStorage.setItem(KEY, theme)
  } catch { /* the theme still applies for this session */ }
}

export function nextTheme(theme: Theme): Theme {
  return ORDER[(ORDER.indexOf(theme) + 1) % ORDER.length]
}
