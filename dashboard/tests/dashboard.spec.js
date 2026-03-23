const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

// Create test data before tests run
const testData = {
  updated_at: new Date().toISOString(),
  summary: {
    total_trades: 10, wins: 8, losses: 2,
    win_rate: 80.0, total_pnl: 500.50, total_staked: 5000, roi: 10.0
  },
  open_positions: [
    { event: 'Test Match A vs B', side: 'NO', market: 'Will B win?',
      stake: 1000, entry_price: 0.85, edge_pct: 3.5,
      score_at_entry: '1-0', minute: 82, profit_if_win: 150, loss_if_lose: 850 }
  ],
  scheduled_bets: [
    { event: 'Big Team vs Small Team', fav_team: 'Big Team',
      underdog_team: 'Small Team', fav_prob: 0.78,
      kickoff: new Date(Date.now() + 3600000).toISOString(),
      bet_at: new Date(Date.now() + 1800000).toISOString() }
  ],
  pnl_curve: [
    { timestamp: new Date(Date.now() - 7200000).toISOString(), event: 'Match 1',
      pnl: 200, cumulative: 200, outcome: 'WIN', side: 'NO',
      market: 'Will X win?', edge_pct: 4.0, stake: 800,
      league: 'EPL', score_at_entry: '0-0', final_score: '0-0' },
    { timestamp: new Date(Date.now() - 3600000).toISOString(), event: 'Match 2',
      pnl: -100, cumulative: 100, outcome: 'LOSS', side: 'YES',
      market: 'Will Y win?', edge_pct: 2.5, stake: 500,
      league: 'La Liga', score_at_entry: '2-0', final_score: '2-1' },
    { timestamp: new Date(Date.now() - 1800000).toISOString(), event: 'Match 3',
      pnl: 400.50, cumulative: 500.50, outcome: 'WIN', side: 'NO',
      market: 'Will Z win?', edge_pct: 5.0, stake: 1000,
      league: 'EPL', score_at_entry: '1-0', final_score: '2-0' },
  ],
  by_league: {
    'EPL': { trades: 2, wins: 2, pnl: 600.50 },
    'La Liga': { trades: 1, wins: 0, pnl: -100 }
  },
  by_day: { '2026-03-22': { trades: 3, wins: 2, pnl: 500.50 } }
};

test.beforeAll(async () => {
  // Write test data file next to index.html
  const dataPath = path.join(__dirname, '..', 'dashboard-data.json');
  fs.writeFileSync(dataPath, JSON.stringify(testData));
});

test.afterAll(async () => {
  const dataPath = path.join(__dirname, '..', 'dashboard-data.json');
  if (fs.existsSync(dataPath)) fs.unlinkSync(dataPath);
});

// ── Page loads ──────────────────────────────────────────

test('page loads without errors', async ({ page }) => {
  const errors = [];
  page.on('pageerror', err => errors.push(err.message));
  await page.goto('/');
  await page.waitForTimeout(2000);
  expect(errors).toEqual([]);
});

test('page title is PolyBot Dashboard', async ({ page }) => {
  await page.goto('/');
  await expect(page).toHaveTitle('PolyBot Dashboard');
});

// ── Summary cards ───────────────────────────────────────

test('PnL card shows correct value', async ({ page }) => {
  await page.goto('/');
  await page.waitForTimeout(2000);
  const pnl = await page.locator('#pnl').textContent();
  expect(pnl).toContain('500');
});

test('record shows 8W-2L', async ({ page }) => {
  await page.goto('/');
  await page.waitForTimeout(2000);
  const record = await page.locator('#record').textContent();
  expect(record).toBe('8W-2L');
});

test('win rate shows 80%', async ({ page }) => {
  await page.goto('/');
  await page.waitForTimeout(2000);
  const wr = await page.locator('#winrate').textContent();
  expect(wr).toBe('80%');
});

test('trades count shows 10', async ({ page }) => {
  await page.goto('/');
  await page.waitForTimeout(2000);
  const trades = await page.locator('#trades').textContent();
  expect(trades).toBe('10');
});

test('open positions count shows 1', async ({ page }) => {
  await page.goto('/');
  await page.waitForTimeout(2000);
  const open = await page.locator('#open').textContent();
  expect(open).toBe('1');
});

// ── PnL toggle ──────────────────────────────────────────

test('PnL card toggles between $ and %', async ({ page }) => {
  await page.goto('/');
  await page.waitForTimeout(2000);
  const card = page.locator('#pnl');

  const before = await card.textContent();
  expect(before).toContain('$');

  await card.click();
  const after = await card.textContent();
  expect(after).toContain('%');
});

// ── Charts render ───────────────────────────────────────

test('PnL chart canvas exists and has content', async ({ page }) => {
  await page.goto('/');
  await page.waitForTimeout(3000);
  const canvas = page.locator('#pnlChart');
  await expect(canvas).toBeVisible();
  // Canvas should have non-zero dimensions (chart rendered)
  const box = await canvas.boundingBox();
  expect(box.width).toBeGreaterThan(100);
  expect(box.height).toBeGreaterThan(50);
});

test('league chart canvas exists', async ({ page }) => {
  await page.goto('/');
  await page.waitForTimeout(3000);
  const canvas = page.locator('#leagueChart');
  await expect(canvas).toBeVisible();
});

// ── Time range buttons ──────────────────────────────────

test('time range buttons exist', async ({ page }) => {
  await page.goto('/');
  await page.waitForTimeout(2000);
  const buttons = page.locator('.time-btn');
  await expect(buttons).toHaveCount(5);
});

test('clicking 24H button changes active state', async ({ page }) => {
  await page.goto('/');
  await page.waitForTimeout(2000);
  const btn = page.locator('.time-btn[data-range="1d"]');
  await btn.click();
  await expect(btn).toHaveClass(/active/);
  // All button should no longer be active
  const allBtn = page.locator('.time-btn[data-range="all"]');
  await expect(allBtn).not.toHaveClass(/active/);
});

// ── Open positions ──────────────────────────────────────

test('open positions table is visible with data', async ({ page }) => {
  await page.goto('/');
  await page.waitForTimeout(2000);
  const section = page.locator('#openSection');
  await expect(section).toBeVisible();
  const rows = page.locator('#openTable tbody tr');
  await expect(rows).toHaveCount(1);
  // Should show market question
  const text = await rows.first().textContent();
  expect(text).toContain('Will B win');
});

// ── Scheduled bets ──────────────────────────────────────

test('scheduled bets table is visible', async ({ page }) => {
  await page.goto('/');
  await page.waitForTimeout(2000);
  const section = page.locator('#schedSection');
  await expect(section).toBeVisible();
  const rows = page.locator('#schedTable tbody tr');
  await expect(rows).toHaveCount(1);
  const text = await rows.first().textContent();
  expect(text).toContain('Big Team');
  expect(text).toContain('Small Team');
});

// ── Trade history ───────────────────────────────────────

test('trade history shows all trades', async ({ page }) => {
  await page.goto('/');
  await page.waitForTimeout(2000);
  const rows = page.locator('#tradesTable tbody tr');
  await expect(rows).toHaveCount(3);
});

test('trade history shows market question in Bet column', async ({ page }) => {
  await page.goto('/');
  await page.waitForTimeout(2000);
  const firstRow = page.locator('#tradesTable tbody tr').first();
  const text = await firstRow.textContent();
  expect(text).toContain('Will X win');
});

test('trade history filter by league works', async ({ page }) => {
  await page.goto('/');
  await page.waitForTimeout(2000);
  await page.selectOption('#filterLeague', 'EPL');
  await page.waitForTimeout(500);
  const rows = page.locator('#tradesTable tbody tr');
  await expect(rows).toHaveCount(2); // only EPL trades
});

test('trade history filter by outcome works', async ({ page }) => {
  await page.goto('/');
  // Wait for trades table to render before filtering
  await page.waitForFunction(() => {
    return document.querySelectorAll('#tradesTable tbody tr').length > 0;
  }, { timeout: 15000 });
  await page.selectOption('#filterOutcome', 'LOSS');
  await page.waitForTimeout(500);
  const rows = page.locator('#tradesTable tbody tr');
  await expect(rows).toHaveCount(1);
  const text = await rows.first().textContent();
  expect(text).toContain('LOSS');
});

// ── Updated timestamp ───────────────────────────────────

test('updated timestamp is shown', async ({ page }) => {
  await page.goto('/');
  // Wait for data to load and render — WebKit on CI can be very slow
  await page.waitForFunction(() => {
    const el = document.getElementById('updated');
    return el && el.textContent && el.textContent.length > 5;
  }, { timeout: 25000 });
  const updated = await page.locator('#updated').textContent();
  expect(updated.length).toBeGreaterThan(5);
});

// ── Mobile responsive ───────────────────────────────────

test('mobile: cards are 2-column grid', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 }); // iPhone
  await page.goto('/');
  await page.waitForTimeout(2000);
  const cards = page.locator('.cards');
  const style = await cards.evaluate(el => getComputedStyle(el).gridTemplateColumns);
  // Should be 2 columns on mobile
  const cols = style.split(' ').filter(s => s !== '').length;
  expect(cols).toBe(2);
});

test('mobile: page has no horizontal overflow', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto('/');
  await page.waitForTimeout(2000);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
  expect(overflow).toBe(false);
});

// ── No console errors on mobile ─────────────────────────

test('mobile: no JS errors', async ({ page }) => {
  const errors = [];
  page.on('pageerror', err => errors.push(err.message));
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto('/');
  await page.waitForTimeout(3000);
  expect(errors).toEqual([]);
});
