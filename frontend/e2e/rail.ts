import type { Locator, Page } from '@playwright/test'
import { railSections, sectionChrome, sectionOfTab, tabAliases } from '../src/lib/tabs'

// Below this width `index.css` hides the rail and shows the drawer toggle.
const DRAWER_BREAKPOINT = 900

/** A name match that tolerates the trailing count badge a row may carry. */
function rowName(label: string): RegExp {
  return new RegExp(`^${label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*\\d*$`)
}

/**
 * Open a rail destination the way a person does: the group first, then the
 * destination inside it. Works from the desktop rail or the mobile drawer.
 *
 * The grouping is read from the app's own tab table rather than restated here,
 * so a destination that changes section changes for these tests too, instead of
 * failing later with a confusing "button not found".
 *
 * Three details of the rail shape this helper:
 *
 * - A group row and the destination it leads to share a label, so the two are
 *   told apart structurally — the row by its class, the destination by the list
 *   it sits in — rather than by name.
 * - An already-open group is left open. Clicking its row again would collapse
 *   it and hide the very destination being opened.
 * - The destination is always clicked explicitly. Opening a group navigates to
 *   its lead on the desktop rail, but leaning on that would do nothing at all
 *   when the group happened to be open already.
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
  // currently on screen. Probing the rail's visibility instead looks equivalent
  // and is not: while a modal is open the rest of the page is `aria-hidden`, so
  // the rail drops out of the accessibility tree and a desktop run would head
  // for a drawer toggle that never appears.
  const width = page.viewportSize()?.width ?? DRAWER_BREAKPOINT + 1
  const inDrawer = width <= DRAWER_BREAKPOINT
  let scope: Locator = page.getByRole('navigation', { name: 'Main navigation' })
  if (inDrawer) {
    await page.getByRole('button', { name: 'Open navigation' }).click()
    scope = page.getByRole('dialog', { name: 'Main navigation' })
  }

  const groupRow = scope.locator('.rail-group').filter({ hasText: rowName(group.label) }).first()
  // A one-destination group is its own row, with nothing to expand.
  if (members.length === 1) {
    await groupRow.click()
    return
  }
  if ((await groupRow.getAttribute('aria-expanded')) !== 'true') {
    await groupRow.click()
  }
  // Then always click the destination itself. Opening the group navigates to
  // its lead as a side effect, but relying on that would silently do nothing
  // when the group was already open — and clicking the destination is what the
  // caller actually asked for either way.
  await scope.getByRole('list', { name: group.label })
    .getByRole('button', { name: rowName(target) })
    .click()
}
