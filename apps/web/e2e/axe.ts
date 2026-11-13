import AxeBuilder from "@axe-core/playwright";
import { expect, type Page } from "@playwright/test";

/** WCAG 2.2 AA rules, including colour contrast. */
export const AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

/** Put the page in one theme without reloading it, so the same state (an open dialog, a filled form) is checked in both.
 *  The emulated system setting changes with it, so the app's System listener (src/lib/theme.ts) agrees instead of
 *  switching back during a scan. Then wait for colour transitions to end, so axe never samples a colour midway. */
export async function showTheme(page: Page, theme: "light" | "dark") {
  await page.emulateMedia({ colorScheme: theme });
  await page.evaluate(async (t) => {
    document.documentElement.dataset.theme = t;
    const frame = () => new Promise((done) => requestAnimationFrame(done));
    await frame();
    // Colour transitions and the dialog's open animation must end first, or axe reads colours midway.
    for (let i = 0; i < 100 && document.getAnimations().some((a) => a.playState === "running" || a.pending); i++) await frame();
    await frame();
  }, theme);
  await expect.poll(() => page.evaluate(() => document.documentElement.dataset.theme)).toBe(theme);
}

type AxeNode = { target: unknown[]; any: { data?: unknown }[] };
/** The element, plus the colours axe measured for a contrast failure. */
function describe(n: AxeNode) {
  const data = n.any.find((c) => c.data && typeof c.data === "object" && "contrastRatio" in c.data)?.data as
    { fgColor: string; bgColor: string; contrastRatio: number } | undefined;
  return n.target.join(" ") + (data ? ` ${data.fgColor} on ${data.bgColor} = ${data.contrastRatio}` : "");
}

/** axe (WCAG 2.2 AA) on the page as it is, in the light theme and then in the dark theme (both themes pass on every
 *  page). The page is left in the theme it was in. */
export async function expectNoViolations(page: Page, what: string) {
  const before = await page.evaluate(() => document.documentElement.dataset.theme ?? "light");
  const found: string[] = [];
  for (const theme of ["light", "dark"] as const) {
    await showTheme(page, theme);
    const results = await new AxeBuilder({ page }).withTags(AXE_TAGS).analyze();
    found.push(...results.violations.map((v) => `${theme}: ${v.id}: ${v.help} (${v.nodes.slice(0, 3).map(describe).join(", ")})`));
  }
  await showTheme(page, before === "dark" ? "dark" : "light");
  expect(found, `${what} has accessibility violations`).toEqual([]);
}
