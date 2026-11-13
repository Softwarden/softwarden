import { expectNoViolations } from "./axe";
import { expect, test, type Page } from "@playwright/test";
import { pageAs, post, token } from "./helpers";

/** Entra discovery on the simulated tenant (configure, run, link, create, dismiss, rerun), the record's
 *  Discovery sources panel, and Entra sign-in through the simulated identity provider. Nothing contacts Microsoft. */

// axe runs in the light and the dark theme at every check (e2e/axe.ts), so these tests get three minutes.
test.describe.configure({ mode: "serial", timeout: 180_000 });

/** Queue a run and run it at once, as the worker would within a minute. */
async function runNow(page: Page) {
  await page.getByRole("button", { name: /^Run now/ }).click();
  await expect(page.getByRole("status").filter({ hasText: "Run queued" })).toBeVisible();
  const ran = await post(page, "e2e/run-connectors", {});
  expect(ran.length).toBe(1);
  await page.reload();
}

/** Contoso starts with no connector and no Microsoft connection. In CI a failed first attempt leaves its own behind in
 *  the same database (a connection named "Microsoft Entra", so the retry's "Add and test" was refused as a duplicate
 *  name), so the journey removes them first, through the API as an Admin would. Records linked or created from a
 *  connector stay; its candidates and runs go with it. */
async function removeDiscovery(page: Page) {
  const list = async (path: string) => (await (await page.request.get(`/api/v1/${path}`)).json()) as { id: string }[];
  for (const c of await list("connectors")) await post(page, `connectors/${c.id}`, undefined, "DELETE");
  for (const c of await list("connections/microsoft")) await post(page, `connections/microsoft/${c.id}`, undefined, "DELETE");
}

const row = (page: Page, name: string) => page.getByRole("row").filter({ has: page.getByRole("rowheader", { name: new RegExp(`^${name}`) }) });

test("An Admin connects the simulated Entra tenant, tests the connection, runs it, and sees skipped apps counted", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "admin@example.test");
  await removeDiscovery(page);
  await page.goto("/discovery");
  await expect(page.getByText("No discovery source is set up")).toBeVisible();
  await expectNoViolations(page, "Discovery, no connector");
  await page.getByRole("link", { name: "Set up a discovery source" }).click();
  await page.getByRole("link", { name: "Set up Microsoft Entra ID" }).click();
  await page.getByRole("button", { name: "Continue to Connect" }).click();
  // The connector signs in through a connection, which must pass its test before any run.
  await page.getByRole("button", { name: "Add a Microsoft connection" }).click();
  const dialog = page.getByRole("dialog", { name: "Add a Microsoft connection" });
  await dialog.getByRole("button", { name: "Add and test" }).click();
  await expect(dialog.getByRole("alert").filter({ hasText: "Fix these fields" })).toBeVisible();
  await expectNoViolations(page, "Connection dialog with errors");
  await dialog.getByLabel("Directory (tenant) ID (required)").fill("simulated");
  await dialog.getByLabel("Client secret (required)").fill("synthetic-secret");
  // CI: the list's reload after the test can land after the test's answer is on screen. Continue right then was refused
  // ("Test Microsoft Entra first") because the list still had the untested copy. Hold every reload of the list until
  // the walkthrough is at Scope, so the answer must count on its own (fails on the old code).
  let release = () => {};
  const held = new Promise<void>((resolve) => { release = resolve; });
  const list = "**/api/v1/connections/microsoft";
  await page.route(list, async (route) => {
    if (route.request().method() === "GET") await held;
    await route.fallback();
  });
  await dialog.getByRole("button", { name: "Add and test" }).click();
  await expect(page.getByTestId("connection-test-result")).toContainText("Microsoft Entra: connection works.");
  await page.getByRole("button", { name: "Save and continue to Scope" }).click();
  await expect(page.getByRole("heading", { name: "Scope", exact: true })).toBeVisible();
  release();
  await page.unroute(list);
  await page.getByRole("button", { name: "Save and continue to Check access" }).click();
  await page.getByRole("button", { name: "Check access", exact: true }).click();
  await expect(page.getByTestId("access-check")).toContainText("Every required permission works.");
  await page.getByRole("button", { name: "Continue to Finish" }).click();
  await page.getByRole("button", { name: "Run now" }).click();
  await expect(page.getByTestId("run-progress")).toContainText("Queued");
  const ran = await post(page, "e2e/run-connectors", {});
  expect(ran.length).toBe(1);
  await expect(page.getByTestId("run-result")).toBeVisible();
  await page.getByRole("link", { name: "Review the candidates" }).click();
  await expect(page.getByText(/15 seen, 9 kept \(skipped: .*4 published by Microsoft/)).toBeVisible();
  await expect(page.getByRole("button", { name: /^To decide 9/ })).toBeVisible();
  await expect(page.getByRole("rowheader", { name: /Microsoft Teams/ })).toHaveCount(0);
  await page.goto("/discovery");
  await expectNoViolations(page, "Discovery with candidates");

  // The source object's cell has its vertical padding, and the row's three actions stay on one line at
  // 1280 px and wider.
  for (const width of [1280, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    const first = page.getByRole("region", { name: "Discovery candidates table" }).getByRole("row").nth(1);
    const pad = await first.getByRole("rowheader").evaluate((th) => parseFloat(getComputedStyle(th).paddingTop));
    expect(pad).toBeGreaterThanOrEqual(8);
    const tops = await first.getByTestId("candidate-actions").getByRole("button").evaluateAll((els) => els.map((el) => Math.round(el.getBoundingClientRect().top)));
    expect(tops.length).toBe(3);
    expect(new Set(tops).size, `actions on one line at ${width} px`).toBe(1);
  }
  // The app ID is shortened, whole for screen readers, and copyable; the result is said in the status line.
  const appId = page.getByRole("region", { name: "Discovery candidates table" }).getByRole("row").nth(1).getByTestId("app-id");
  await expect(appId.locator("code")).toHaveText(/^[0-9a-f]{8}…$/);
  await expect(appId).toContainText(/full ID [0-9a-f-]{36}/);
  await appId.getByRole("button", { name: /^Copy the app ID of / }).click();
  await expect(page.getByRole("status").filter({ hasText: /app ID of .+ (copied|is [0-9a-f-]{36})/i })).toBeVisible();
});

test("An Editor links, creates and dismisses; a rerun brings none of them back", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const t = token();
  const app = await post(page, "applications", { name: `${t} Zoom Meetings`, lifecycle: "active" });
  await page.goto("/discovery");

  await row(page, "Zoom").getByRole("button", { name: /^Link to record[: ]+Zoom$/ }).click();
  const link = page.getByRole("dialog", { name: "Link to an existing record" });
  await expectNoViolations(page, "Link dialog");
  await link.getByRole("combobox").fill(t);
  await page.getByRole("option", { name: new RegExp(`${t} Zoom Meetings`) }).click();
  await link.getByRole("button", { name: "Link", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "Linked Zoom to" })).toContainText(`Linked Zoom to ${t} Zoom Meetings. Nothing on ${t} Zoom Meetings changed.`);

  await row(page, "ServiceNow").getByRole("button", { name: /^Create record/ }).click();
  const create = page.getByRole("dialog", { name: "Create a record from this application" });
  await create.getByLabel("Name").fill(`${t} ServiceNow`);
  await create.getByRole("button", { name: "Create and link" }).click();
  await expect(page.getByRole("status").filter({ hasText: "and linked it." })).toContainText(`Created ${t} ServiceNow and linked it.`);

  await row(page, "Graph Explorer Test").getByRole("button", { name: /^Dismiss/ }).click();
  const dismiss = page.getByRole("dialog", { name: "Dismiss this candidate" });
  await dismiss.getByRole("button", { name: "Dismiss" }).click();
  await expect(dismiss.getByRole("alert")).toContainText("Say why");
  await dismiss.getByLabel("Why dismiss it?").fill("A developer's test registration");
  await dismiss.getByRole("button", { name: "Dismiss" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Dismissed Graph Explorer Test." })).toBeVisible();
  await expect(page.getByRole("button", { name: /^To decide 6/ })).toBeVisible();

  // Undo puts a decision back; doing it again restores the state.
  await page.getByRole("button", { name: "Undo" }).click();
  await expect(page.getByRole("button", { name: /^To decide 7/ })).toBeVisible();
  await row(page, "Graph Explorer Test").getByRole("button", { name: /^Dismiss/ }).click();
  await page.getByLabel("Why dismiss it?").fill("A developer's test registration");
  await page.getByRole("dialog").getByRole("button", { name: "Dismiss" }).click();
  await expect(page.getByRole("button", { name: /^To decide 6/ })).toBeVisible();

  // A rerun (an Admin's) brings nothing back and changes nothing on the linked record.
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  await admin.goto("/discovery");
  await runNow(admin);
  await expect(admin.getByText(/0 new, 0 changed, 0 no longer in source/)).toBeVisible();
  await page.reload();
  await expect(page.getByRole("button", { name: /^To decide 6/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /^Linked 2/ })).toBeVisible();

  // The linked record shows its source and that the source's name differs; the curated name is unchanged.
  await page.goto(`/applications/${app.id}`);
  await expect(page.getByRole("heading", { name: `${t} Zoom Meetings`, level: 1 })).toBeVisible();
  const sources = page.getByRole("region", { name: "Discovery sources" });
  await expect(sources).toContainText("Zoom");
  await expect(sources).toContainText("Source differs");
  await expectNoViolations(page, "Record with Discovery sources");
  // Unlinking is explicit: a confirmation saying the record stays.
  await sources.getByRole("button", { name: /^Unlink/ }).click();
  const unlink = page.getByRole("dialog", { name: "Undo the decision on Zoom" });
  await expect(unlink).toContainText(`${t} Zoom Meetings stays as it is.`);
  await unlink.getByRole("button", { name: "Undo decision" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Undone. Zoom is a candidate again" })).toBeVisible();
});

test("Viewers see candidates but can't decide; Needs attention carries one row per connector", async ({ browser, baseURL }) => {
  const viewer = await pageAs(browser, baseURL, "viewer@example.test");
  await viewer.goto("/discovery");
  await expect(viewer.getByRole("region", { name: "Discovery candidates table" })).toBeVisible();
  await expect(viewer.getByRole("button", { name: /^Link / })).toHaveCount(0);
  await expect(viewer.getByRole("button", { name: /^Run now/ })).toHaveCount(0);
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  await admin.goto("/?scope=all");
  const queue = admin.getByRole("region", { name: /^Discovery candidates \(/ });
  await expect(queue).toContainText(/new candidates? to link, create or dismiss/);
  await queue.getByRole("link", { name: /Review candidates/ }).click();
  await expect(admin).toHaveURL(/\/discovery\?connector=/);
});

test("Sign in with Microsoft Entra ID (simulated provider); unknown identities are refused", async ({ browser, baseURL }) => {
  // Sign-in settings are per organization, and the installation's env Entra config applies only while there is
  // one organization (the e2e database has several by now). The hook gives the seed organization active Entra settings
  // on the simulator; its sign-in link (/login?org=<slug>) offers the button.
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  const { org } = (await post(admin, "e2e/sso-seed", {})) as { org: string };
  const signInLink = `/login?${new URLSearchParams({ org, next: "/renewals" })}`;
  const context = await browser.newContext({ baseURL });
  const page = await context.newPage();
  await page.goto(signInLink);
  await expect(page.getByRole("heading", { name: "Sign in to Softwarden" })).toBeVisible();
  await expectNoViolations(page, "Sign-in with the Entra button");
  await page.getByRole("link", { name: "Sign in with Microsoft Entra ID" }).click();
  await expect(page.getByRole("heading", { name: "Simulated identity provider" })).toBeVisible();
  await page.getByLabel("Sign in as (email)").fill("stranger@elsewhere.test");
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("alert")).toContainText("doesn't match a Softwarden account");
  // The refusal keeps the organization (?org=), so the button is back for another try.
  await expect(page).toHaveURL(/\/login\?error=no_account&org=[^&]+&next=%2Frenewals/);
  const retry = page.getByRole("link", { name: "Sign in with Microsoft Entra ID" });
  await expect(retry).toBeVisible();
  await retry.click();
  await page.getByLabel("Sign in as (email)").fill("editor@example.test");
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("heading", { name: "Renewals", level: 1 })).toBeVisible(); // back where they started
  // Signed in as the editor: the user menu at the bottom of the sidebar names them.
  await expect(page.getByRole("button", { name: /Sam Editor · Editor/ })).toBeVisible();

  // With no page asked for, Entra sign-in lands on the Dashboard, everyone's home.
  const fresh = await browser.newContext({ baseURL });
  const home = await fresh.newPage();
  await home.goto(`/login?${new URLSearchParams({ org })}`);
  const entra = home.getByRole("link", { name: "Sign in with Microsoft Entra ID" });
  await expect(entra).toHaveAttribute("href", `/api/v1/auth/oidc/start?${new URLSearchParams({ org })}&next=%2Fdashboard`);
  await entra.click();
  await home.getByLabel("Sign in as (email)").fill("viewer@example.test");
  await home.getByRole("button", { name: "Continue" }).click();
  await expect(home).toHaveURL(/\/dashboard$/);
  await expect(home.getByRole("heading", { name: "Dashboard", level: 1 })).toBeVisible();
  await fresh.close();

  // The Admin sees the link and can undo it.
  await admin.goto("/admin/users");
  const editorRow = admin.getByRole("row").filter({ hasText: "editor@example.test" });
  await expect(editorRow).toContainText("Signs in with Entra");
  // One identity at a time, behind a confirmation.
  await editorRow.getByRole("button", { name: /^Unlink Entra/ }).click();
  const confirm = admin.getByRole("dialog", { name: /^Unlink Entra from Sam Editor\?/ });
  await confirm.getByRole("button", { name: "Unlink Entra" }).click();
  await expect(confirm).toHaveCount(0);
  await expect(editorRow).not.toContainText("Signs in with Entra");
  await context.close();
});
