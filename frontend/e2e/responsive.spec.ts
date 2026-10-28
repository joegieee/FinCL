import { expect, test } from '@playwright/test'
import { railSections, tabMeta } from '../src/lib/tabs'
import { resetLedger } from './ledger'
import { openTab } from './rail'

// Prove that a normal URL works without a key or an existing session.
test.use({ storageState: { cookies: [], origins: [] } })
test.beforeAll(async ({ request }) => {
  await resetLedger(request, 'reconciled-history')
})

for (const width of [320, 390, 768, 1280]) {
  test(`all destinations fit a ${width}px viewport without a launch key`, async ({ page }) => {
    test.skip(test.info().project.name !== 'desktop')
    test.setTimeout(180_000)
    await page.setViewportSize({ width, height: 844 })
    for (const tab of railSections.flatMap(section => section.tabs)) {
      await test.step(tab, async () => {
        await page.goto(`/?view=${encodeURIComponent(tab)}&start=2026-01-01&end=2026-09-30`)
        await expect(page.getByRole('heading', { name: tabMeta[tab].title, exact: true }).first()).toBeVisible()
        await expect(page.locator('footer')).toBeVisible()
        await expect(page.locator('.skeleton-page')).toHaveCount(0)
        await expect(page.locator('.alert.error')).toHaveCount(0)
        const overflow = await page.evaluate(() => ({
          width: document.documentElement.scrollWidth,
          elements: Array.from(document.querySelectorAll('main *')).filter(el => el.getBoundingClientRect().right > innerWidth + 1)
            .slice(0, 10).map(el => `${el.tagName}.${el.className}`),
        }))
        expect.soft(overflow.width, `${tab}: ${overflow.elements.join(', ')}`).toBeLessThanOrEqual(width + 1)
      })
    }
    if (width === 320) {
      await page.goto('/?view=Planning')
      await page.getByRole('button', { name: 'New budget', exact: true }).click()
      await expect(page.getByLabel('Monthly limit')).toBeVisible()
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width + 1)
    }
    expect(await page.context().cookies()).toEqual([])
  })
}

test('phone navigation and transaction dialog remain usable', async ({ page }) => {
  test.skip(test.info().project.name !== 'narrow')
  await page.goto('/?start=2026-01-01&end=2026-09-30')
  await expect(page.getByRole('heading', { name: 'A clearer picture.' })).toBeVisible()
  await openTab(page, 'Transactions')
  await expect(page.getByRole('dialog', { name: 'Main navigation' })).toHaveCount(0)
  await page.locator('.transactions-table .category-chip').first().click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  const bounds = await dialog.boundingBox()
  expect(bounds!.x).toBeGreaterThanOrEqual(0)
  expect(bounds!.y).toBeGreaterThanOrEqual(0)
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(390)
  expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(844)
  await expect(dialog.getByLabel('Category')).toBeVisible()
  await page.screenshot({ path: test.info().outputPath('mobile-dialog.png') })
  await dialog.getByRole('button', { name: 'Close', exact: true }).click()
  await expect(dialog).toHaveCount(0)
  await page.reload()
  await expect(page.getByRole('heading', { name: 'Every recorded transaction.' })).toBeVisible()
})
