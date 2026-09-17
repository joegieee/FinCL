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
export async function openTab(page: Page, name: string): Promise<void> {
  const section = sectionOfTab[name]
  if (!section) throw new Error(`${name} is not a rail destination`)
  const group = sectionChrome[section]

  const rail = page.getByRole('navigation', { name: 'Main navigation' })
  const inDrawer = !(await rail.isVisible())
  let scope: Locator = rail
  if (inDrawer) {
    await page.getByRole('button', { name: 'Open navigation' }).click()
    scope = page.getByRole('dialog', { name: 'Main navigation' })
  }

  await scope.getByRole('button', { name: group.label, exact: true }).click()
  if (inDrawer || group.lead !== name) {
    await scope.getByRole('button', { name, exact: true }).click()
  }
}
