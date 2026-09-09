import { describe, expect, it } from 'vitest'
import { commandDestinations, railSections, railTabs, tabAliases, tabMeta } from './tabs'

describe('rail metadata', () => {
  it('lists every section-bearing tab in the rail exactly once', () => {
    const withSection = Object.keys(tabMeta).filter(key => tabMeta[key].section)
    expect(new Set(railTabs).size).toBe(railTabs.length)
    expect([...railTabs].sort()).toEqual([...withSection].sort())
  })

  it('groups each rail tab into exactly one section', () => {
    for (const key of railTabs) {
      const owners = railSections.filter(section => section.tabs.includes(key))
      expect(owners).toHaveLength(1)
    }
  })

  it('keeps aliases out of the rail but reachable from the command palette', () => {
    for (const alias of Object.keys(tabAliases)) {
      expect(tabMeta[alias].section).toBeUndefined()
      expect(railTabs).not.toContain(alias)
      expect(commandDestinations).toContain(alias)
      expect(railTabs).toContain(tabAliases[alias])
    }
  })
})
