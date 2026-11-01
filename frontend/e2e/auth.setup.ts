import { expect, test as setup } from '@playwright/test'

// Normal serving needs no launch key or session cookie.
const authFile = 'e2e/.auth.json'

setup('open the website without a launch key', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible()
  await page.context().storageState({ path: authFile })
})
