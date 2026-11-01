import type { Locator, Page } from '@playwright/test'
import { railSections, sectionChrome, sectionOfTab, tabAliases, tabLabel } from '../src/lib/tabs'

// At or below this width the app swaps the sidebar for a section bar and page tabs.
const COMPACT_BREAKPOINT = 900

/** A name match that tolerates the trailing count badge a row may carry. */
function rowName(label: string): RegExp {
  return new RegExp(`^${label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*\\d*$`)
}

/**
 * Open a rail destination the way a person does: the group first, then the
 * destination inside it. Works from the sidebar or the section bar and page tabs.
 *
 * The grouping is read from the app's own tab table rather than restated here,
 * so a destination that changes section changes for these tests too, instead of
 * failing later with a confusing "button not found".
 *
 * A section row and the destination it leads to share a label, so the two are
 * told apart structurally: the row by its class, the destination by the list it
 * sits in. The destination is always clicked explicitly. Opening a section lands
 * on its lead, but the caller asked for this page, and an open section's row
 * does not need clicking at all.
 */
export async function openTab(page: Page, name: string): Promise<void> {
  // An alias ("Money Owed", "Cash Flow") is reachable by URL and the command
  // palette but is not a rail row of its own, so it is opened through the
  // destination it resolves to.
  const target = tabAliases[name] ?? name
  const section = sectionOfTab[target]
  if (!section) throw new Error(`${name} is not a rail destination`)
  const group = sectionChrome[section]
  const members = railSections.find(item => item.name === section)?.tabs ?? []

  // Which surface the rail is on is a property of the viewport, not of what is
  // currently on screen. While a modal is open the rest of the page is
  // `aria-hidden`, so probing for the sidebar would wrongly report it gone.
  const width = page.viewportSize()?.width ?? COMPACT_BREAKPOINT + 1
  if (width <= COMPACT_BREAKPOINT) {
    await page.getByRole('navigation', { name: 'Sections' }).getByRole('button', { name: new RegExp(`^${group.label}`) }).click()
    if (members.length > 1) {
      await page.getByRole('navigation', { name: `${group.label} pages` }).getByRole('button', { name: rowName(tabLabel(target)) }).click()
    }
    return
  }

  const scope: Locator = page.getByRole('navigation', { name: 'Main navigation' })
  const groupRow = scope.locator('.rail-group').filter({ hasText: rowName(group.label) }).first()
  // A one-destination group is its own row, with nothing to expand.
  if (members.length === 1) {
    await groupRow.click()
    return
  }
  if ((await groupRow.getAttribute('aria-expanded')) !== 'true') {
    await groupRow.click()
  }
  await scope.getByRole('list', { name: group.label })
    .getByRole('button', { name: rowName(tabLabel(target)) })
    .click()
}
