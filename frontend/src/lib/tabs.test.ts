import { describe, expect, it } from 'vitest'
import { commandDestinations, railSections, railTabs, sectionChrome, sectionOfTab, tabAliases, tabMeta } from './tabs'

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

  it('gives every group a lead destination that is one of its own tabs', () => {
    for (const section of railSections) {
      expect(section.tabs).toContain(section.lead)
    }
  })

  it('names a group for every section, so no group renders unlabelled', () => {
    for (const section of railSections) {
      expect(sectionChrome[section.name].label).toBe(section.label)
      expect(section.label.length).toBeGreaterThan(0)
    }
  })

  it('maps every rail destination back to exactly the section that holds it', () => {
    for (const section of railSections) {
      for (const tab of section.tabs) {
        expect(sectionOfTab[tab]).toBe(section.name)
      }
    }
    expect(Object.keys(sectionOfTab).sort()).toEqual([...railTabs].sort())
  })
})
