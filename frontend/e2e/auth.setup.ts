import { expect, test as setup } from '@playwright/test'

// The bootstrap secret is single-use by design, so exchange it for a session
// cookie once here and let every test project reuse the stored state.
const authFile = 'e2e/.auth.json'

setup('bootstrap the loopback session', async ({ page }) => {
  await page.goto('/#bootstrap=e2e-secret')
  await expect(page.getByRole('heading', { name: 'A clearer picture.' })).toBeVisible()
  await page.context().storageState({ path: authFile })
})
