// Browser-only verification tooling; Playwright is installed in npm's cache,
// never added to the application's runtime dependencies.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { chromium } = require(process.env.SPOTTER_PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const base = process.env.SPOTTER_BASE_URL || 'http://127.0.0.1:8011';
  const browser = await chromium.launch({
    executablePath: process.env.SPOTTER_EDGE_PATH || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    headless: true,
  });
  const errors = [];
  const report = { checks: {}, javascript_errors: errors };
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(base, { waitUntil: 'domcontentloaded' });
    const expectedMpg = Number(process.env.SPOTTER_EXPECTED_MPG || 10);
    assert.ok((await page.locator('header').textContent()).includes(`${expectedMpg} mpg`));
    await page.locator('#start').fill('Chicago, IL');
    await page.locator('#finish').fill('Dallas, TX');
    const responsePromise = page.waitForResponse(r => r.url().endsWith('/api/v1/route/') && r.request().method() === 'POST');
    await page.locator('#go').click();
    const response = await responsePromise;
    assert.equal(response.status(), 201);
    const body = await response.json();
    assert.equal(body.assumptions.vehicle_mpg, expectedMpg);
    assert.equal(body.assumptions.tank_capacity_gallons, 500 / expectedMpg);
    await page.waitForFunction(() => window.FuelRoute?.plan?.plan_id);
    await page.waitForTimeout(600);
    assert.equal(await page.evaluate(() => window.FuelRoute.markers.length), body.fuel_stops.length);
    assert.ok(await page.locator('.leaflet-overlay-pane path').count() >= 2);
    assert.ok(await page.locator('.legend').isVisible());
    await page.locator('.stop[data-i="0"] .scard').click();
    await page.locator('.leaflet-popup').waitFor({ state: 'visible' });
    report.checks.desktop_route_markers_legend_stop_popup = true;
    const before = await page.locator('#panel').evaluate(el => el.getBoundingClientRect().width);
    const grip = await page.locator('#resizer').boundingBox();
    await page.mouse.move(grip.x + grip.width / 2, grip.y + grip.height / 2);
    await page.mouse.down();
    await page.mouse.move(grip.x + grip.width / 2 + 70, grip.y + grip.height / 2, { steps: 8 });
    await page.mouse.up();
    const after = await page.locator('#panel').evaluate(el => el.getBoundingClientRect().width);
    assert.ok(after - before > 30);
    report.checks.draggable_divider = { before, after };
    await page.screenshot({ path: 'var/browser_desktop.png', fullPage: true });
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.waitForFunction(id => window.FuelRoute?.plan?.plan_id === id, body.plan_id);
    assert.ok((await page.locator('#json-btn').getAttribute('href')).endsWith(`/api/v1/route/${body.plan_id}/`));
    report.checks.saved_plan_reload_and_json_link = true;
    await page.setViewportSize({ width: 600, height: 900 });
    await page.locator('#tab-map').click();
    await page.waitForTimeout(300);
    assert.ok(await page.locator('#map').isVisible());
    await page.screenshot({ path: 'var/browser_mobile.png', fullPage: true });
    await page.locator('#tab-list').click();
    assert.ok(await page.locator('#result').isVisible());
    report.checks.mobile_map_list_tabs = true;
    report.checks.loaded_map_tiles = await page.locator('.leaflet-tile-loaded').count();
    assert.ok(report.checks.loaded_map_tiles > 0, 'No base-map tile loaded');
    const fallback = await browser.newContext({ viewport: { width: 1100, height: 850 } });
    await fallback.route('**/leaflet.js', route => route.abort());
    const fallbackPage = await fallback.newPage();
    fallbackPage.on('pageerror', error => errors.push(error.message));
    await fallbackPage.goto(body.map_url, { waitUntil: 'domcontentloaded' });
    assert.ok((await fallbackPage.locator('#map-msg').textContent()).includes('could not be loaded'));
    assert.ok(await fallbackPage.locator('#result').isVisible());
    report.checks.map_library_failure_keeps_trip_details = true;
    await page.goto(base, { waitUntil: 'domcontentloaded' });
    await page.locator('#start').fill('Toronto, ON');
    await page.locator('#finish').fill('Dallas, TX');
    const errorResponse = page.waitForResponse(r => r.url().endsWith('/api/v1/route/') && r.request().method() === 'POST');
    await page.locator('#go').click();
    assert.equal((await errorResponse).status(), 422);
    await page.locator('#error').waitFor({ state: 'visible' });
    assert.ok((await page.locator('#error').textContent()).includes('location_outside_usa'));
    report.checks.controlled_error_display = true;
    assert.deepEqual(errors, []);
    report.passed = true;
    fs.mkdirSync('validation', { recursive: true });
    fs.writeFileSync(process.env.SPOTTER_BROWSER_REPORT || 'validation/browser_checks.json', JSON.stringify(report, null, 2) + '\n');
    console.log(JSON.stringify(report, null, 2));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
