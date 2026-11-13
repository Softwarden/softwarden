// Renders og/og-image.html to public/og.png (1200 x 630).
// Uses the Playwright installed for the web app (../apps/web), so the site has no Playwright dependency.
// Run from site/: node og/make-og.cjs
const path = require('path');
const { pathToFileURL } = require('url');
const { chromium } = require(path.resolve(__dirname, '../../apps/web/node_modules/playwright'));

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1200, height: 630 } });
  await page.goto(pathToFileURL(path.join(__dirname, 'og-image.html')).href);
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(__dirname, '../public/og.png') });
  await browser.close();
  console.log('Wrote public/og.png');
})();
