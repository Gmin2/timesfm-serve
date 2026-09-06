import { expect, test } from '@playwright/test'

test('real archive renders, station/run controls and timezone work', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', e => errors.push(e.message))
  await page.goto('/')
  await expect(page.locator('.recharts-line-curve')).toHaveCount(3)
  await expect(page.locator('.recharts-area-area')).toHaveCount(1)
  await expect(page.getByRole('heading', { name: 'GuwahatiStation forecast' })).toBeVisible()
  await page.getByRole('button', { name: 'Previous forecast run', exact: true }).click()
  await expect(page.getByRole('combobox', { name: 'Forecast run' })).toContainText('10 Aug 2026')
  await page.getByRole('button', { name: 'UTC', exact: true }).click()
  await expect(page.getByRole('combobox', { name: 'Forecast run' })).toContainText('06:00')
  await page.getByRole('button', { name: 'Table view', exact: true }).click()
  await expect(page.locator('tbody tr')).toHaveCount(48)
  await page.getByRole('button', { name: '24h', exact: true }).click()
  await expect(page.locator('tbody tr')).toHaveCount(24)
  await page.getByRole('link', { name: /Hyderabad/ }).click()
  await expect(page.locator('h1')).toContainText('Hyderabad')
  await expect(page.getByRole('combobox', { name: 'Forecast run' })).toContainText('18 Aug 2026')
  expect(errors).toEqual([])
})

test('series controls, tooltip and download use plotted values', async ({ page }) => {
  await page.goto('/')
  await expect(page.locator('.recharts-line-curve')).toHaveCount(3)
  await page.getByRole('checkbox', { name: 'Ridge correction', exact: true }).check()
  await expect(page.locator('.recharts-line-curve')).toHaveCount(4)
  await page.getByRole('checkbox', { name: 'p10–p90 range', exact: true }).uncheck()
  await expect(page.locator('.recharts-area-area')).toHaveCount(0)
  await page.locator('.forecast-chart').hover({ position: { x: 220, y: 150 } })
  await expect(page.locator('.chart-tooltip')).toBeVisible()
  const download = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Export CSV', exact: true }).click()
  expect((await download).suggestedFilename()).toBe('42410099999_20260818T0600Z.csv')
})

test('benchmarks show the baseline, Ridge and rejected cases', async ({ page }) => {
  await page.goto('/benchmarks')
  await expect(page.getByRole('heading', { name: 'Forecast benchmarks', exact: true })).toBeVisible()
  await expect(page.locator('.scores-section tbody tr')).toHaveCount(9)
  await expect(page.getByText('Ridge slightly outperformed TimesFM on RMSE.', { exact: false })).toBeVisible()
  await page.getByRole('link', { name: /Run archive/ }).click()
  await expect(page.locator('tbody tr')).toHaveCount(99)
  await expect(page.locator('.run-status.rejected')).toHaveCount(9)
  await page.getByRole('link', { name: 'Open forecast', exact: false }).first().click()
  await expect(page.locator('.recharts-line-curve')).toHaveCount(3)
})

test('unavailable live mode never displays replay values', async ({ page }) => {
  await page.route('**/api/connection', route => route.fulfill({ json: { configured: true } }))
  await page.route('**/api/v1/weather/stations/*/latest', route => route.fulfill({ status: 503, json: { detail: 'live_forecast_stale' } }))
  await page.route('**/api/v1/weather/stations/*/status', route => route.fulfill({ json: {
    available: false, station_id: '42410099999', checked_at: '2026-09-06T06:00:00Z', ingestion: null, job: null,
  } }))
  await page.goto('/forecasts?mode=live')
  await expect(page.getByRole('heading', { name: 'The latest forecast has expired', exact: true })).toBeVisible()
  await expect(page.locator('.recharts-line-curve')).toHaveCount(0)
  await expect(page.getByText('Saved holdout', { exact: true })).toHaveCount(0)
  await page.getByRole('tab', { name: 'Historical', exact: true }).click()
  await expect(page.locator('.recharts-line-curve')).toHaveCount(3)
})

for (const width of [375, 768, 1440]) {
  test('responsive layout at ' + width, async ({ page }) => {
    await page.setViewportSize({ width, height: 920 })
    await page.goto('/')
    await expect(page.locator('.recharts-line-curve')).toHaveCount(3)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
    const box = await page.locator('.forecast-chart').boundingBox()
    expect(box!.width).toBeGreaterThan(250)
    await page.screenshot({ path: '../tmp/dashboard-' + width + '.png', fullPage: true, animations: 'disabled' })
    await page.getByRole('link', { name: 'Benchmarks', exact: true }).click()
    await expect(page.locator('.scores-section tbody tr')).toHaveCount(9)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
    await page.screenshot({ path: '../tmp/benchmarks-' + width + '.png', fullPage: true, animations: 'disabled' })
  })
}
