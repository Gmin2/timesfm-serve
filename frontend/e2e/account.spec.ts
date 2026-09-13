import { expect, test, type Page } from '@playwright/test'

const account = { id: 1234, github_id: '1234', login: 'forecast-developer', rate_limit_per_min: 60, expires_at: '2026-09-13T06:00:00Z' }
const created = { id: 77, prefix: 'tfm_aaaaaa', label: 'My weather app', created_at: '2026-09-06T06:00:00Z', revoked_at: null, scope: 'weather:read' }
const secret = 'tfm_' + 'a'.repeat(32)

async function fixture(page: Page, signedIn = true) {
  let loggedIn = signedIn
  const keys: (typeof created & { revoked_at: string | null })[] = []
  await page.route('**/api/auth/session', route => route.fulfill({ json: { configured: true, user: loggedIn ? account : null, csrf_token: loggedIn ? 'test-csrf' : null,
    api_origin: 'https://weather.example.test', max_active_keys: 3 } }))
  await page.route('**/api/v1/account/keys', route => {
    if (route.request().method() === 'POST') {
      expect(route.request().headers()['x-csrf-token']).toBe('test-csrf')
      expect(route.request().postDataJSON()).toEqual({ label: 'My weather app' })
      keys.push(created)
      return route.fulfill({ status: 201, json: { ...created, key: secret } })
    }
    return route.fulfill({ json: { keys } })
  })
  await page.route('**/api/v1/account/keys/77', route => {
    expect(route.request().method()).toBe('DELETE')
    expect(route.request().headers()['x-csrf-token']).toBe('test-csrf')
    keys[0] = { ...created, revoked_at: '2026-09-06T07:00:00Z' }
    return route.fulfill({ status: 204 })
  })
  await page.route('**/api/auth/logout', route => {
    expect(route.request().method()).toBe('POST')
    loggedIn = false
    return route.fulfill({ status: 204 })
  })
}

async function createKey(page: Page) {
  await page.getByRole('button', { name: 'Create key', exact: true }).click()
  await page.getByLabel('Key name', { exact: true }).fill('My weather app')
  await page.getByRole('button', { name: 'Continue', exact: true }).click()
  await expect(page.getByRole('dialog')).toContainText('@forecast-developer')
  await page.getByRole('button', { name: 'Generate key', exact: true }).click()
}

test('signed-out access uses the local Nucleo GitHub asset', async ({ page }) => {
  await fixture(page, false)
  await page.goto('/api-keys')
  const login = page.getByRole('link', { name: 'Continue with GitHub', exact: true })
  await expect(login).toHaveAttribute('href', '/api/auth/github')
  const logo = login.locator('img')
  expect(await logo.evaluate((image: HTMLImageElement) => image.complete && image.naturalWidth > 0)).toBe(true)
  await expect(page.getByText('No repository permissions')).toBeVisible()
})

test('create, dismiss, revoke and logout work without retaining the raw key', async ({ page }) => {
  await fixture(page)
  await page.goto('/api-keys')
  await expect(page.getByText('No API keys yet.', { exact: true })).toBeVisible()
  await createKey(page)
  await expect(page.locator('.secret-value')).toContainText(secret)
  expect(await page.evaluate(() => JSON.stringify({ local: localStorage, session: sessionStorage }))).not.toContain(secret)
  await page.getByRole('button', { name: 'Done', exact: true }).click()
  await expect(page.getByText(secret, { exact: true })).toHaveCount(0)
  await page.getByRole('button', { name: 'Python', exact: true }).click()
  await expect(page.locator('.request-code')).toContainText('os.environ["WEATHER_API_KEY"]')
  await page.getByRole('button', { name: 'Revoke My weather app', exact: true }).click()
  await expect(page.getByRole('dialog')).toBeVisible()
  await page.getByRole('button', { name: 'Cancel', exact: true }).click()
  await expect(page.getByText('Active', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Revoke My weather app', exact: true }).click()
  await page.getByRole('button', { name: 'Revoke key', exact: true }).click()
  await expect(page.getByText('Revoked', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Sign out', exact: true }).click()
  await expect(page.getByRole('link', { name: 'Continue with GitHub', exact: true })).toBeVisible()
  await expect(page.getByLabel('Key name', { exact: true })).toHaveCount(0)
})

test('expired session cannot leave an API key management screen active', async ({ page }) => {
  await fixture(page)
  await page.goto('/api-keys')
  await expect(page.getByText('No API keys yet.', { exact: true })).toBeVisible()
  await page.route('**/api/v1/account/keys', route => route.fulfill({ status: 401, json: { detail: 'session_expired' } }))
  await createKey(page)
  await expect(page.getByRole('link', { name: 'Continue with GitHub', exact: true })).toBeVisible()
})

test('account page is independent of archive availability and reports OAuth failures', async ({ page }) => {
  await fixture(page, false)
  await page.route('**/data/catalog.json', route => route.fulfill({ status: 503, json: {} }))
  await page.goto('/api-keys?auth_error=github_access_denied')
  await expect(page.getByText('GitHub sign-in was cancelled.', { exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Continue with GitHub', exact: true })).toBeVisible()
})

for (const width of [375, 768, 1440]) {
  test('account layout at ' + width, async ({ page }) => {
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.setViewportSize({ width, height: 920 })
    await fixture(page)
    await page.goto('/api-keys')
    await expect(page.getByText('No API keys yet.', { exact: true })).toBeVisible()
    await createKey(page)
    await expect(page.locator('.secret-value')).toContainText(secret)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
    await page.screenshot({ path: '../tmp/account-' + width + '.png', fullPage: true, animations: 'disabled' })
    expect(errors).toEqual([])
  })
}
