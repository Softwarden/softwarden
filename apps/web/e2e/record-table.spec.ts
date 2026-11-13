import { expect, test } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { pageAs, post, token } from "./helpers";

/** RecordTable and FilterBar on the Applications list: server sort, filters and page in the URL,
 *  keyset pagination, per-user column choice and density, selection with the bulk bar, CSV export of the current
 *  filter, and the loading, error, empty and viewer states. axe runs in light and dark at each state. */

test.describe.configure({ mode: "serial", timeout: 180_000 });

// Search is fuzzy (trigram similarity), so other tests' "Dt…" records match a token search. These tests narrow the
// list by a tag made for the test instead (the tag filter is exact).

test("Sort, filter chips and pagination live in the URL and survive a reload", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "editor@example.test");
  for (const [name, criticality] of [["Alpha", "high"], ["Bravo", "low"], ["Charlie", "low"], ["Delta", "medium"]]) {
    await post(page, "applications", { name: `${t} ${name}`, lifecycle: "active", criticality, tags: [`Srt${t}`] });
  }
  const only = `tag=Srt${t}`;
  await page.goto(`/applications?q=${t}&${only}`);
  const table = page.getByRole("region", { name: "Applications table" });
  const names = () => table.getByRole("rowheader").getByRole("link").allInnerTexts();
  await expect(table.getByRole("link", { name: `${t} Delta` })).toBeVisible();

  // While searching, rows are by best match: no column claims the order.
  const nameHeader = table.getByRole("columnheader", { name: "Name" });
  await expect(nameHeader).toHaveAttribute("aria-sort", "none");
  await expect(page.getByRole("navigation", { name: "Pagination" })).toContainText("Sorted by best match, then name.");

  // Without a search the default order is by name, and the Name header says so.
  await page.goto(`/applications?${only}`);
  await expect(nameHeader).toHaveAttribute("aria-sort", "ascending");
  await expect(page.getByRole("navigation", { name: "Pagination" })).toContainText("Sorted by name, A to Z.");
  await expect.poll(names).toEqual([`${t} Alpha`, `${t} Bravo`, `${t} Charlie`, `${t} Delta`]);

  // Sort by name: ascending, then descending; the server sorts (ordering=…) and the URL keeps it.
  await page.goto(`/applications?q=${t}&${only}`);
  await expect(nameHeader).toHaveAttribute("aria-sort", "none");
  await nameHeader.getByRole("button", { name: "Name" }).click();
  await expect(page).toHaveURL(/ordering=name/);
  await expect(nameHeader).toHaveAttribute("aria-sort", "ascending");
  await expect.poll(names).toEqual([`${t} Alpha`, `${t} Bravo`, `${t} Charlie`, `${t} Delta`]);
  await nameHeader.getByRole("button", { name: "Name" }).click();
  await expect(page).toHaveURL(/ordering=-name/);
  await expect(nameHeader).toHaveAttribute("aria-sort", "descending");
  await expect.poll(names).toEqual([`${t} Delta`, `${t} Charlie`, `${t} Bravo`, `${t} Alpha`]);
  await expect(page.getByRole("navigation", { name: "Pagination" })).toContainText("Sorted by name, Z to A.");
  await expect(table.getByRole("columnheader", { name: "Lifecycle" })).toHaveAttribute("aria-sort", "none");
  await expect(table.getByRole("columnheader", { name: "Review state" })).not.toHaveAttribute("aria-sort", /.*/); // not sortable on the server
  await page.reload();
  await expect(table.getByRole("columnheader", { name: "Name" })).toHaveAttribute("aria-sort", "descending");
  await expect.poll(names).toEqual([`${t} Delta`, `${t} Charlie`, `${t} Bravo`, `${t} Alpha`]);

  // Keyset pagination: two per page, Next and Previous with the cursor in the URL.
  await page.goto(`/applications?${only}&ordering=name&page_size=2`);
  await expect.poll(names).toEqual([`${t} Alpha`, `${t} Bravo`]);
  const pager = page.getByRole("navigation", { name: "Pagination" });
  await expect(pager.getByRole("button", { name: "Previous page" })).toBeDisabled();
  await pager.getByRole("button", { name: "Next page" }).click();
  await expect(page).toHaveURL(/cursor=.+&page=2|page=2.*cursor=/);
  await expect.poll(names).toEqual([`${t} Charlie`, `${t} Delta`]);
  await expect(pager).toContainText("Page 2");
  await expect(pager).toContainText(`Showing applications 3–4 of 4.`);
  await expect(pager.getByRole("button", { name: "Next page" })).toBeDisabled();
  await page.reload();
  await expect.poll(names).toEqual([`${t} Charlie`, `${t} Delta`]);
  await pager.getByRole("button", { name: "Previous page" }).click();
  await expect.poll(names).toEqual([`${t} Alpha`, `${t} Bravo`]);
  await expect(page).not.toHaveURL(/cursor=/);

  // A filter chip: Add filter, Criticality, High. The preset's meaning stays as chips; the page goes back to the first.
  await pager.getByRole("button", { name: "Next page" }).click();
  await expect(page).toHaveURL(/page=2/);
  await page.getByRole("button", { name: "Add filter" }).click();
  await expectNoViolations(page, "Applications list with the Add filter menu open");
  await page.getByRole("menuitem", { name: "Criticality" }).click();
  const criticality = page.getByLabel("Criticality", { exact: true });
  await expect(criticality).toBeFocused();
  await criticality.selectOption("high");
  await expect(page).toHaveURL(/criticality=high/);
  await expect(page).toHaveURL(/lifecycle=active%2Cdeprecated/);
  await expect(page).not.toHaveURL(/cursor=|page=2/);
  await expect(page.getByRole("button", { name: "Active", exact: true })).toHaveAttribute("aria-pressed", "false");
  await expect.poll(names).toEqual([`${t} Alpha`]);
  await page.reload();
  await expect(page.getByLabel("Criticality", { exact: true })).toHaveValue("high");
  await expect.poll(names).toEqual([`${t} Alpha`]);
  await expectNoViolations(page, "Applications list with filter chips");

  // CSV export of the current filter (the export.csv endpoint with the same query).
  await page.getByRole("button", { name: "Export CSV…" }).click();
  const dialog = page.getByRole("dialog", { name: "Export CSV" });
  await expect(dialog).toContainText("Criticality: high");
  const [download] = await Promise.all([page.waitForEvent("download"), dialog.getByRole("button", { name: "Download CSV" }).click()]);
  const csv = (await (await import("node:fs/promises")).readFile((await download.path())!)).toString("utf8");
  expect(csv).toContain(`${t} Alpha`);
  expect(csv).not.toContain(`${t} Bravo`);

  // Removing the chip removes the parameter and returns focus to Add filter.
  await page.getByRole("button", { name: "Remove Criticality filter" }).click();
  await expect(page).not.toHaveURL(/criticality=/);
  await expect(page.getByRole("button", { name: "Add filter" })).toBeFocused();
  await expect.poll(names).toEqual([`${t} Alpha`, `${t} Bravo`]);

  // Clear filters goes back to the default view.
  await page.getByRole("button", { name: "Clear filters" }).first().click();
  await expect(page).not.toHaveURL(/lifecycle=|q=|tag=/);
  await expect(page.getByRole("button", { name: "Active", exact: true })).toHaveAttribute("aria-pressed", "true");
});

test("Column choice and density are kept per user; the viewer gets them without selection", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "admin@example.test");
  await post(page, "applications", { name: `${t} Gamma`, lifecycle: "active", criticality: "critical" });
  await page.goto(`/applications?q=${t}`);
  const table = page.getByRole("region", { name: "Applications table" });
  await expect(table.getByRole("link", { name: `${t} Gamma` })).toBeVisible();
  await expect(table.getByRole("columnheader", { name: "Criticality" })).toHaveCount(0); // hidden by default

  // Columns: a menu of checkable items that stays open while toggling.
  const columns = page.getByRole("button", { name: /^Columns/ });
  await columns.click();
  const menu = page.getByRole("menu", { name: "Show columns" });
  await expect(menu.getByRole("menuitemcheckbox", { name: "Criticality" })).toHaveAttribute("aria-checked", "false");
  await expect(menu.getByRole("menuitemcheckbox", { name: "Name" })).toHaveCount(0); // the row header can't be hidden
  await expectNoViolations(page, "Column picker open");
  await menu.getByRole("menuitemcheckbox", { name: "Criticality" }).click();
  await menu.getByRole("menuitemcheckbox", { name: "Gaps" }).click();
  await expect(menu.getByRole("menuitemcheckbox", { name: "Criticality" })).toHaveAttribute("aria-checked", "true");
  await page.keyboard.press("Escape");
  await expect(menu).toHaveCount(0);
  await expect(columns).toBeFocused();
  await expect(table.getByRole("columnheader", { name: "Criticality" })).toBeVisible();
  await expect(table.getByRole("row", { name: new RegExp(`${t} Gamma`) })).toContainText("Critical");
  await expect(table.getByRole("columnheader", { name: "Gaps" })).toHaveCount(0);
  // Criticality sorts by meaning (the API ranks it), and the order is said in those words.
  await table.getByRole("columnheader", { name: "Criticality" }).getByRole("button").click();
  await expect(page).toHaveURL(/ordering=criticality/);
  await expect(page.getByRole("navigation", { name: "Pagination" })).toContainText("Sorted by criticality, most to least critical.");
  await table.getByRole("columnheader", { name: "Criticality" }).getByRole("button").click();
  await expect(page.getByRole("navigation", { name: "Pagination" })).toContainText("Sorted by criticality, least to most critical.");

  // Density: compact rows are 44 px, comfortable 60 px.
  const density = page.getByRole("group", { name: "Row density" });
  await expect(density.getByRole("button", { name: "Comfortable" })).toHaveAttribute("aria-pressed", "true");
  const row = table.getByRole("row", { name: new RegExp(`${t} Gamma`) });
  expect(Math.round((await row.boundingBox())!.height)).toBeGreaterThanOrEqual(60);
  await density.getByRole("button", { name: "Compact" }).click();
  await expect(density.getByRole("button", { name: "Compact" })).toHaveAttribute("aria-pressed", "true");
  await expect.poll(async () => Math.round((await row.boundingBox())!.height)).toBeLessThanOrEqual(45);
  await expectNoViolations(page, "Applications list, compact, with a chosen column");

  // Both survive a reload, and are stored for this user and table only.
  await page.reload();
  await expect(table.getByRole("columnheader", { name: "Criticality" })).toBeVisible();
  await expect(page.getByRole("group", { name: "Row density" }).getByRole("button", { name: "Compact" })).toHaveAttribute("aria-pressed", "true");
  const keys = await page.evaluate(() => Object.keys(localStorage).filter((k) => k.includes(".table.")).sort());
  expect(keys).toEqual(expect.arrayContaining([expect.stringMatching(/^softwarden\.\d+\.table\.applications\.columns$/), expect.stringMatching(/^softwarden\.\d+\.table\.applications\.density$/)]));
  // Components keep their own choice.
  await page.goto(`/applications?kind=components`);
  await expect(page.getByRole("group", { name: "Row density" }).getByRole("button", { name: "Comfortable" })).toHaveAttribute("aria-pressed", "true");

  // Reset to default columns.
  await page.goto(`/applications?q=${t}`);
  await page.getByRole("button", { name: /^Columns/ }).click();
  await page.getByRole("menuitem", { name: "Reset to default columns" }).click();
  await expect(table.getByRole("columnheader", { name: "Criticality" })).toHaveCount(0);
  await expect(table.getByRole("columnheader", { name: "Gaps" })).toBeVisible();

  // A Viewer: no checkboxes and no bulk bar, but columns and density work.
  const viewer = await pageAs(browser, baseURL, "viewer@example.test");
  await viewer.goto(`/applications?q=${t}`);
  await expect(viewer.getByRole("region", { name: "Applications table" }).getByRole("link", { name: `${t} Gamma` })).toBeVisible();
  await expect(viewer.getByRole("checkbox")).toHaveCount(0);
  await expect(viewer.getByRole("button", { name: /^Columns/ })).toBeVisible();
  await expect(viewer.getByRole("group", { name: "Row density" })).toBeVisible();
  await expect(viewer.getByRole("link", { name: "Add application" })).toHaveCount(0);
  await expectNoViolations(viewer, "Applications list as a Viewer");
});

test("Selection: keyboard selection, the bulk bar replaces the header actions, announced, cleared with focus kept", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "editor@example.test");
  for (const n of ["One", "Two", "Three"]) await post(page, "applications", { name: `${t} ${n}`, lifecycle: "active", tags: [`Sel${t}`] });
  await page.goto(`/applications?tag=Sel${t}&ordering=name`);
  const table = page.getByRole("region", { name: "Applications table" });
  await expect(table.getByRole("link", { name: `${t} Two` })).toBeVisible();
  await expect(page.getByRole("region", { name: "Bulk actions" })).toHaveCount(0);

  // Keyboard: focus a row's checkbox and press Space.
  await page.getByRole("checkbox", { name: `Select ${t} One` }).focus();
  await page.keyboard.press("Space");
  await expect(page.getByRole("checkbox", { name: `Select ${t} One` })).toBeChecked();
  await expect(page.getByTestId("table-announcer")).toHaveText("1 selected. Bulk actions are above the table.");
  await page.keyboard.press("Tab"); // the row's link, then the next row's checkbox
  await page.keyboard.press("Tab");
  await expect(page.getByRole("checkbox", { name: `Select ${t} Three` })).toBeFocused();
  await page.keyboard.press("Space");

  const bar = page.getByRole("region", { name: "Bulk actions" });
  await expect(bar).toContainText("2 selected");
  // Where the bar is is said once, when it appears; after that just the count.
  await expect(page.getByTestId("table-announcer")).toHaveText("2 selected.");
  await expect(page.getByRole("heading", { level: 1, name: "Applications" })).toBeVisible(); // the title stays
  await expect(page.getByRole("button", { name: "Export CSV…" })).toHaveCount(0); // header actions are replaced
  await expect(page.getByRole("link", { name: "Add application" })).toHaveCount(0);
  await expect(table.getByRole("row", { name: new RegExp(`${t} One`) })).toHaveAttribute("data-state", "selected");
  await expectNoViolations(page, "Applications list with the bulk bar");

  // Select all on this page, then the bulk edit flow opens for every selected record.
  await page.getByRole("checkbox", { name: "Select all on this page" }).check();
  await expect(bar).toContainText("3 selected");
  await bar.getByRole("button", { name: "Bulk edit…" }).click();
  await expect(page.getByRole("dialog", { name: "Bulk edit 3 applications" })).toBeVisible();
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(bar.getByRole("button", { name: "Bulk edit…" })).toBeFocused();

  // Clearing returns focus to Select all and brings the header actions back.
  await bar.getByRole("button", { name: "Clear selection" }).click();
  await expect(bar).toHaveCount(0);
  await expect(page.getByRole("checkbox", { name: "Select all on this page" })).toBeFocused();
  await expect(page.getByTestId("table-announcer")).toHaveText("Selection cleared.");
  await expect(page.getByRole("button", { name: "Export CSV…" })).toBeVisible();

  // Selection is for the rows on screen: changing the sort clears it.
  await page.getByRole("checkbox", { name: `Select ${t} Two` }).check();
  await table.getByRole("columnheader", { name: "Name" }).getByRole("button").click();
  await expect(page).toHaveURL(/ordering=-name/);
  await expect(page.getByRole("region", { name: "Bulk actions" })).toHaveCount(0);
});

test("Loading, error with retry, and the empty filter state", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "editor@example.test");
  await post(page, "applications", { name: `${t} Solo`, lifecycle: "active", tags: [`St${t}`] });

  // Loading: skeleton rows with a polite announcement.
  let release!: () => void;
  const held = new Promise<void>((done) => { release = done; });
  await page.route(/\/api\/v1\/applications\?.*tag=/, async (route) => { await held; await route.continue(); });
  await page.goto(`/applications?tag=St${t}`);
  await expect(page.getByText("Loading applications…")).toBeAttached();
  release();
  await expect(page.getByRole("region", { name: "Applications table" }).getByRole("link", { name: `${t} Solo` })).toBeVisible();
  await page.unroute(/\/api\/v1\/applications\?.*tag=/);

  // Error: what failed, that nothing changed, and Retry.
  await page.route(/\/api\/v1\/applications\?.*tag=/, (route) => route.fulfill({
    status: 500, contentType: "application/problem+json", body: JSON.stringify({ type: "about:blank", title: "Server error", status: 500, detail: "The list couldn't be read." }),
  }));
  await page.reload();
  const alert = page.getByRole("alert").filter({ hasText: "The list couldn't be read." });
  await expect(alert).toContainText("Nothing was changed.");
  await expectNoViolations(page, "Applications list, failed to load");
  await page.unroute(/\/api\/v1\/applications\?.*tag=/);
  await alert.getByRole("button", { name: "Retry" }).click();
  await expect(page.getByRole("region", { name: "Applications table" }).getByRole("link", { name: `${t} Solo` })).toBeVisible();

  // No rows with filters: say so and offer Clear filters.
  await page.goto(`/applications?tag=St${t}&review_state=confirmed`); // Solo is unreviewed
  await expect(page.getByText("No applications match these filters")).toBeVisible();
  await expect(page.getByRole("button", { name: "Clear filters" })).toHaveCount(2); // in the filter bar and in the empty state
  await expectNoViolations(page, "Applications list, no matches");
});

test("Narrow screens and 200 % zoom: the table scrolls in its region and the name column leaves room", async ({ browser, baseURL }) => {
  const t = token();
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  await post(editor, "applications", { name: `${t} A rather long application name that has to wrap`, lifecycle: "active", tags: [`Nw${t}`] });
  const me = await (await editor.request.get("/api/v1/auth/me")).json();
  const state = await editor.context().storageState();
  // 320 px is a phone (touch, coarse pointer); 640 px is also a 1280 px window at 200 % zoom.
  for (const width of [320, 640]) {
    const context = await browser.newContext({ baseURL, storageState: state, viewport: { width, height: 800 }, hasTouch: width === 320, isMobile: width === 320 });
    const page = await context.newPage();
    // Every column shown, so the table is wider than the screen.
    await page.addInitScript((key) => localStorage.setItem(key, "[]"), `softwarden.${me.user.id}.table.applications.columns`);
    await page.goto(`/applications?tag=Nw${t}`);
    const region = page.getByRole("region", { name: "Applications table" });
    await expect(region.getByRole("link", { name: new RegExp(`${t} A rather long`) })).toBeVisible();
    const size = await region.evaluate((el) => ({
      scrolls: el.scrollWidth > el.clientWidth,
      region: el.clientWidth,
      header: el.querySelector('th[scope="row"]')!.getBoundingClientRect().width,
    }));
    expect(size.scrolls, `${width} px: the table scrolls inside its region`).toBe(true);
    expect(size.header, `${width} px: the row header leaves room`).toBeLessThan(size.region * 0.6);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), "no sideways page scroll").toBe(true);
    if (width === 640) {
      // From 640 px the name stays in view while the table scrolls sideways.
      await region.evaluate((el) => { el.scrollLeft = el.scrollWidth; });
      const box = (await region.boundingBox())!;
      const header = (await region.locator('th[scope="row"]').first().boundingBox())!;
      expect(header.x).toBeLessThan(box.x + 60);
      expect(header.x).toBeGreaterThanOrEqual(box.x - 1);
    } else {
      // Touch targets are 44 px on phones: the tag chip's remove button and the density buttons.
      expect(await page.evaluate(() => matchMedia("(pointer: coarse)").matches)).toBe(true);
      for (const target of [page.getByRole("button", { name: "Remove Tag filter" }), page.getByRole("button", { name: "Compact" })]) {
        const b = (await target.boundingBox())!;
        expect(Math.round(b.height)).toBeGreaterThanOrEqual(44);
      }
      expect(Math.round((await page.getByRole("button", { name: "Remove Tag filter" }).boundingBox())!.width)).toBeGreaterThanOrEqual(44);
    }
    await expectNoViolations(page, `Applications list at ${width} px`);
    await context.close();
  }
});
