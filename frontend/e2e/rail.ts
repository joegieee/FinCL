import type { Locator, Page } from '@playwright/test'
import { sectionChrome, sectionOfTab } from '../src/lib/tabs'

/**
 * Open a rail destination the way a person does: the group first, then the
 * destination inside it. Works from the desktop rail or the mobile drawer.
 *
 * The grouping is read from the app's own tab table rather than restated here,
 * so a destination that changes section changes for these tests too, instead of
 * failing later with a confusing "button not found".
 *
 * The two surfaces differ in one way that matters here. On the desktop rail a
 * group row also navigates to its lead destination, so asking for that lead
 * needs no second click. In the drawer a group row only expands, because
 * navigating closes the drawer — so every destination there takes both clicks.
 */
// Below this width `index.css` hides the rail and shows the drawer toggle.
const DRAWER_BREAKPOINT = 900

/** A name match that tolerates the trailing count badge a row may carry. */
function rowName(label: string): RegExp {
  return new RegExp(`^${label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*\\d*$`)
}

export async function openTab(page: Page, name: string): Promise<void> {
  const section = sectionOfTab[name]
  if (!section) throw new Error(`${name} is not a rail destination`)
  const group = sectionChrome[section]

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

  // Rows carry a pending count beside their label ("Money 6"), so an exact
  // name match would miss exactly the rows that most need visiting.
  await scope.getByRole('button', { name: rowName(group.label) }).click()
  if (inDrawer || group.lead !== name) {
    await scope.getByRole('button', { name: rowName(name) }).click()
  }
}
