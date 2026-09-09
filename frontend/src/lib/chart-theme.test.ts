import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const HEX_LITERAL = /#[0-9a-fA-F]{3,8}\b/g

function source(relative: string) {
  return readFileSync(fileURLToPath(new URL(relative, import.meta.url)), 'utf8')
}

describe('chart colour theme', () => {
  it('is the only place a chart hex literal is declared', () => {
    // chart-theme.ts itself declares the fallback-free CSS var names; the hex
    // values live in index.css (light and dark), not in component code.
    for (const file of ['../features/analysis/TrendChart.tsx', '../features/forecast/index.tsx', '../features/overview/SpendingDonut.tsx']) {
      const hexLiterals = source(file).match(HEX_LITERAL) ?? []
      expect(hexLiterals, `${file} should reference chart colours via var(--chart-*), found ${JSON.stringify(hexLiterals)}`).toHaveLength(0)
    }
  })

  it('declares every categorical, semantic, base and conservative colour as a CSS custom property', () => {
    const css = source('../index.css')
    for (const name of ['--chart-1', '--chart-2', '--chart-3', '--chart-4', '--chart-5', '--chart-6', '--chart-7', '--chart-8', '--chart-base', '--chart-conservative']) {
      const lightCount = (css.match(new RegExp(`${name}:`, 'g')) ?? []).length
      expect(lightCount, `${name} should be declared for both light and dark`).toBeGreaterThanOrEqual(2)
    }
  })
})
