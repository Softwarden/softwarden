import { expect, test } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { pageAs, post, token } from "./helpers";

/** The catalog field additions round-trip through the application and component forms, show on the
 *  detail page and in the list's optional columns and filter chips; vendor details, team members and tag colours. */

const GUID = "6f0b2a52-1c1e-4d1a-9a55-6c3f8a2b9d10";

test("Application fields: category, deployment, version, URLs, authentication, SSO/SCIM, Entra IDs, department, classification", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const t = token();
  await page.goto("/applications/new");
  await expect(page.getByRole("heading", { level: 1, name: "Add application" })).toBeVisible();
  await page.getByLabel("Name (required)").fill(`${t} Ledger`);
  await page.getByLabel("Category").selectOption({ label: "ERP and finance" });
  await page.getByLabel("Deployment model").selectOption({ label: "SaaS" });
  await page.getByLabel("Version").fill("2026.1");
  await page.getByLabel("Authentication method").selectOption({ label: "SAML" });
  await page.getByLabel("Single sign-on (SSO)").selectOption({ label: "Yes, SSO is on" });
  await page.getByLabel("SCIM provisioning").selectOption({ label: "No SCIM" });
  await page.getByLabel("Primary URL").fill("https://ledger.example/app");
  await page.getByLabel("Admin URL").fill("https://ledger.example/admin");
  await page.getByLabel("Application (client) ID").fill("not-a-guid");
  await page.getByLabel("Department").selectOption({ label: "Finance" });
  await page.getByLabel("Cost center").fill("CC-4100");
  await page.getByLabel("Data classification").selectOption({ label: "Confidential" });
  await page.getByRole("button", { name: "Save" }).click();
  // The API's field error is shown inline, with the summary; nothing typed is lost.
  const summary = page.getByRole("alert").filter({ hasText: "Fix this field" });
  await expect(summary).toContainText("GUID");
  await expect(page.getByLabel("Application (client) ID")).toHaveAttribute("aria-invalid", "true");
  await expect(page.getByLabel("Version")).toHaveValue("2026.1");
  await expectNoViolations(page, "Application form with a field error");
  await page.getByLabel("Application (client) ID").fill(GUID);
  await page.getByLabel("Service principal object ID").fill(GUID.replace("6f0b", "7a1c"));
  await page.getByRole("button", { name: "Save" }).click();

  await expect(page).toHaveURL(/\/applications\/[0-9a-f-]{36}$/);
  const id = page.url().split("/").pop()!;
  const details = page.getByRole("region", { name: "Details" });
  for (const text of ["ERP and finance", "SaaS", "2026.1", "SAML", "ledger.example/app", "ledger.example/admin", GUID, "CC-4100", "Confidential"]) await expect(details).toContainText(text);
  await expect(details.getByRole("definition").filter({ hasText: /^Yes$/ })).toHaveCount(1); // SSO
  await expect(details.getByRole("definition").filter({ hasText: /^No$/ })).toHaveCount(1); // SCIM
  await expectNoViolations(page, "Application detail with catalog fields");

  // The edit form starts from the saved values (round trip).
  await page.getByRole("link", { name: `Edit ${t} Ledger` }).click();
  await expect(page.getByLabel("Deployment model")).toHaveValue("saas");
  await expect(page.getByLabel("Single sign-on (SSO)")).toHaveValue("true");
  await expect(page.getByLabel("SCIM provisioning")).toHaveValue("false");
  await expect(page.getByLabel("Application (client) ID")).toHaveValue(GUID);
  await expect(page.getByLabel("Cost center")).toHaveValue("CC-4100");
  // Not recorded is a third state, sent as null.
  await page.getByLabel("SCIM provisioning").selectOption({ label: "Not recorded" });
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page).toHaveURL(`/applications/${id}`);
  const saved = await (await page.request.get(`/api/v1/applications/${id}`)).json();
  expect(saved.scim_provisioning).toBeNull();
  expect(saved.sso_enabled).toBe(true);

  // The list: Category as an optional column and a filter chip.
  const categories = await (await page.request.get("/api/v1/categories")).json();
  const finance = categories.find((c: { name: string }) => c.name === "ERP and finance");
  await page.goto(`/applications?q=${t}&category=${finance.id}&data_classification=confidential`);
  const table = page.getByRole("region", { name: "Applications table" });
  await expect(table.getByRole("link", { name: `${t} Ledger` })).toBeVisible();
  await expect(page.getByLabel("Category", { exact: true })).toHaveValue(finance.id);
  await page.getByRole("button", { name: /^Columns/ }).click();
  await page.getByRole("menuitemcheckbox", { name: "Category" }).click();
  await page.keyboard.press("Escape");
  await expect(table.getByRole("row", { name: new RegExp(`${t} Ledger`) })).toContainText("ERP and finance");
  await page.getByRole("button", { name: "Export CSV…" }).click();
  await expect(page.getByRole("dialog", { name: "Export CSV" })).toContainText("Category: ERP and finance");
  await page.keyboard.press("Escape");
  await page.goto(`/applications?q=${t}&deployment_model=desktop`);
  await expect(table.getByRole("link", { name: `${t} Ledger` })).toHaveCount(0);
});

test("Component fields: new types, hosting, environment, runbook, schedule, Azure and Power Platform IDs", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const t = token();
  await page.goto("/applications/new?kind=component");
  await page.getByLabel("Name (required)").fill(`${t} Provisioner`);
  await page.getByLabel("Component type").selectOption({ label: "Infrastructure automation" });
  await page.getByLabel("Hosting").selectOption({ label: "Azure" });
  await page.getByLabel("Environment", { exact: true }).selectOption({ label: "Production" });
  await page.getByLabel("Schedule or trigger").fill("Nightly at 02:00");
  await page.getByLabel("Runbook URL").fill("https://wiki.example/runbook");
  await page.getByLabel("Azure resource ID").fill("/subscriptions/1/resourceGroups/rg/providers/x");
  await page.getByLabel("Power Platform environment ID").fill("env-42");
  await page.getByLabel("External ID").fill("ext-7");
  await page.getByLabel("Data classification").selectOption({ label: "Internal" });
  await expectNoViolations(page, "Component form, filled");
  await page.getByRole("button", { name: "Save" }).click();

  await expect(page).toHaveURL(/\/components\/[0-9a-f-]{36}$/);
  const id = page.url().split("/").pop()!;
  const runtime = page.getByRole("region", { name: "Runtime" });
  for (const text of ["Infrastructure automation", "Azure", "Production", "Nightly at 02:00", "wiki.example/runbook"]) await expect(runtime).toContainText(text);
  const details = page.getByRole("region", { name: "Details" });
  for (const text of ["/subscriptions/1/resourceGroups/rg/providers/x", "env-42", "ext-7", "Internal"]) await expect(details).toContainText(text);
  await expectNoViolations(page, "Component detail with catalog fields");

  await page.getByRole("link", { name: `Edit ${t} Provisioner` }).click();
  await expect(page.getByLabel("Component type")).toHaveValue("infra_automation");
  await expect(page.getByLabel("Hosting")).toHaveValue("azure");
  await expect(page.getByLabel("Environment", { exact: true })).toHaveValue("prod");
  await expect(page.getByLabel("External ID")).toHaveValue("ext-7");
  await page.getByLabel("Hosting").selectOption({ label: "On-premises" });
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page).toHaveURL(`/components/${id}`);
  await expect(page.getByRole("region", { name: "Runtime" })).toContainText("On-premises");

  await page.goto(`/applications?kind=components&q=${t}&hosting=on_prem`);
  await expect(page.getByRole("region", { name: "Components table" }).getByRole("link", { name: `${t} Provisioner` })).toBeVisible();
  await expect(page.getByLabel("Hosting", { exact: true })).toHaveValue("on_prem");
  await page.goto(`/applications?kind=components&q=${t}&environment=dev`);
  await expect(page.getByText("No components match these filters")).toBeVisible();
});

test("Vendor details, team members and tag colors", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  // CI: the browser is the worker's, and the pages the earlier tests signed in stayed open (Playwright closes only its
  // own `page` fixture's context). Near the end of a shard of ninety tests run by the runner's one worker, 86 pages
  // were still open and every action took seconds, until this test timed out. pageAs now closes the earlier tests'
  // pages: only this one is open.
  expect(browser.contexts()).toEqual([page.context()]);
  const t = token();

  // A vendor with support contacts and an account manager.
  await page.goto("/vendors");
  await page.getByRole("button", { name: "Add vendor" }).click();
  let dialog = page.getByRole("dialog", { name: "Add vendor" });
  await dialog.getByLabel("Name (required)").fill(`${t} Zylker`);
  await dialog.getByLabel("Website").fill("https://zylker.example");
  await dialog.getByLabel("Customer portal").fill("https://portal.zylker.example");
  await dialog.getByLabel("Support email").fill("help@zylker.example");
  await dialog.getByLabel("Support phone").fill("+1 555 0100");
  await dialog.getByRole("combobox", { name: "Account manager" }).fill("Lee Chen");
  await page.getByRole("option", { name: /^Lee Chen/ }).first().click();
  await dialog.getByLabel("Notes").fill("Renews each March.");
  await expectNoViolations(page, "Vendor form");
  await dialog.getByRole("button", { name: "Save" }).click();
  const vendors = page.getByRole("region", { name: "Vendors table" });
  const vrow = vendors.getByRole("row", { name: new RegExp(`${t} Zylker`) });
  await expect(vrow).toContainText("help@zylker.example · +1 555 0100");
  await expect(vrow).toContainText("Lee Chen");
  await vrow.getByRole("button", { name: `Edit ${t} Zylker` }).click();
  dialog = page.getByRole("dialog", { name: `Edit ${t} Zylker` });
  await expect(dialog.getByLabel("Notes")).toHaveValue("Renews each March.");
  await expect(dialog.getByRole("combobox", { name: "Account manager" })).toHaveValue("Lee Chen");
  await dialog.getByRole("button", { name: "Cancel" }).click();

  // A team with two members (Vendors has its own page; teams stay under People & Teams).
  await page.goto("/admin/people?tab=teams");
  await page.getByRole("button", { name: "Add team" }).click();
  dialog = page.getByRole("dialog", { name: "Add team" });
  await dialog.getByLabel("Name (required)").fill(`${t} Payroll Squad`);
  const members = dialog.getByRole("group", { name: "Members" });
  await members.getByRole("checkbox", { name: /^Dana Ortiz/ }).check();
  await members.getByRole("checkbox", { name: /^Priya Raman/ }).check();
  await expect(members).toContainText("Chosen: Dana Ortiz, Priya Raman");
  await expectNoViolations(page, "Team form with members");
  await dialog.getByRole("button", { name: "Save" }).click();
  const teams = page.getByRole("region", { name: "Teams table" });
  await expect(teams.getByRole("row", { name: new RegExp(`${t} Payroll Squad`) })).toContainText("Dana Ortiz, Priya Raman");

  // A tag gets a colour and a description; its chips show both colour and name.
  const app = await post(page, "applications", { name: `${t} Tagged`, lifecycle: "active", tags: [`${t}-fin`] });
  await page.goto("/tags");
  await expect(page.getByRole("heading", { level: 1, name: "Tags" })).toBeVisible();
  const tags = page.getByRole("region", { name: "Tags table" });
  const trow = tags.getByRole("row", { name: new RegExp(`${t}-fin`) });
  await expect(trow).toContainText("Neutral (default)");
  await trow.getByRole("button", { name: `Edit tag ${t}-fin` }).click();
  dialog = page.getByRole("dialog", { name: `Edit tag ${t}-fin` });
  await dialog.getByRole("radio", { name: "Teal" }).check();
  await dialog.getByLabel("Description").fill("Finance systems");
  await expectNoViolations(page, "Tag color dialog");
  await dialog.getByRole("button", { name: "Save tag" }).click();
  await expect(trow).toContainText("Teal");
  await expect(trow).toContainText("Finance systems");
  await expect(trow.locator('[data-tag-color="teal"]')).toHaveText(`${t}-fin`);
  await expectNoViolations(page, "Tags with a teal tag");
  await page.goto(`/applications/${app.id}`);
  await expect(page.getByRole("list", { name: "Tags" }).locator('[data-tag-color="teal"]')).toHaveText(`${t}-fin`);

  // Viewers see tags, vendors and teams without edit controls.
  const viewer = await pageAs(browser, baseURL, "viewer@example.test");
  await viewer.goto("/tags");
  await expect(viewer.getByRole("region", { name: "Tags table" }).getByRole("row", { name: new RegExp(`${t}-fin`) })).toBeVisible();
  await expect(viewer.getByRole("button", { name: /^Edit tag/ })).toHaveCount(0);
  await expectNoViolations(viewer, "Tags as a Viewer");
});
