import { expect, type Browser, test } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { PDF, pageAs, post, token } from "./helpers";

/** End to end in one journey:
 *
 *  "An editor can create a Copilot Studio agent with models, channels, owners, a custom "Business case" field, a
 *  SharePoint link attachment and an uploaded PDF, and every edit appears in its audit log with a diff."
 *  "A viewer cannot upload, edit or download-by-guess (attachment URLs are not predictable and are role-checked)."
 *
 *  Runs in an organization of its own (the Playwright-only `e2e/spec-org` route, `journey`): the "Business case" custom
 *  field is organization-wide. */

const ADMIN = "pj-admin@example.test";
const EDITOR = "pj-editor@example.test";
const VIEWER = "pj-viewer@example.test";

async function setup(browser: Browser, baseURL: string | undefined) {
  const contoso = await pageAs(browser, baseURL, "admin@example.test");
  await post(contoso, "e2e/spec-org", { which: "journey" });
  await contoso.context().close();
  const admin = await pageAs(browser, baseURL, ADMIN);
  const fields: { label: string }[] = await (await admin.request.get("/api/v1/custom-fields")).json();
  if (!fields.some((f) => f.label === "Business case")) {
    await post(admin, "custom-fields", { name: "business_case", label: "Business case", type: "longtext", applies_to: ["ai_agent"], description: "Why the organization runs this agent." });
  }
  await admin.context().close();
}

test("An Editor builds a Copilot Studio agent with models, channels, owners, a Business case, a SharePoint link and a PDF; History shows every edit with a diff; a Viewer can't write or guess", async ({ browser, baseURL }) => {
  test.setTimeout(180_000);
  await setup(browser, baseURL);
  const page = await pageAs(browser, baseURL, EDITOR);
  const t = token();
  const provider = await post(page, "ai-providers", { name: `${t} Labs` });
  await post(page, "ai-models", { provider: provider.id, name: "Orbit", version: "4", modality: "text" });
  await post(page, "ai-models", { provider: provider.id, name: "Lens", version: "1", modality: "multimodal" });

  // 1. Create the agent in the form.
  await page.goto("/ai-agents/new");
  await expect(page.getByRole("heading", { level: 1, name: "Add AI agent" })).toBeVisible();
  await page.getByLabel("Name (required)").fill(`${t} HR assistant`);
  await page.getByLabel("Platform", { exact: true }).selectOption({ label: "Copilot Studio" });
  const models = page.getByRole("group", { name: "Models" });
  await models.getByRole("checkbox", { name: new RegExp(`${t} Labs Orbit 4`) }).check();
  await models.getByRole("checkbox", { name: new RegExp(`${t} Labs Lens 1`) }).check();
  const channels = page.getByRole("group", { name: "Channels" });
  await channels.getByRole("checkbox", { name: "Teams" }).check();
  await channels.getByRole("checkbox", { name: "Microsoft 365 Copilot" }).check();
  await page.getByRole("combobox", { name: "Business owner" }).fill("Ines");
  await page.getByRole("option", { name: /^Ines Okafor/ }).first().click();
  await page.getByRole("combobox", { name: "Technical owner" }).fill("Mateo");
  await page.getByRole("option", { name: /^Mateo Silva/ }).first().click();
  await page.getByLabel("Data access scope").fill("HR SharePoint site, read only");
  await page.getByLabel("Human in the loop").selectOption({ label: "Yes, a person approves" });
  await page.getByLabel("Business case").fill("Answers HR policy questions in Teams, so the HR desk handles fewer tickets.");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page).toHaveURL(/\/ai-agents\/[0-9a-f-]{36}$/);
  const id = page.url().split("/").pop()!;
  const agent = page.getByRole("region", { name: "Agent" });
  await expect(agent).toContainText("Copilot Studio");
  await expect(agent.getByRole("listitem").filter({ hasText: `${t} Labs Orbit 4` })).toBeVisible();
  await expect(agent.getByRole("listitem").filter({ hasText: `${t} Labs Lens 1` })).toBeVisible();
  await expect(agent.getByRole("list", { name: "Channels" })).toHaveText(/Teams\s*Microsoft 365 Copilot/);
  await expect(page.getByRole("region", { name: "Ownership (confirmed fields)" })).toContainText("Ines Okafor");
  await expect(page.getByRole("region", { name: "Ownership (confirmed fields)" })).toContainText("Mateo Silva");
  await expect(page.getByRole("region", { name: "Custom fields" })).toContainText("Answers HR policy questions in Teams");

  // 2. A SharePoint link and an uploaded PDF on its Attachments tab.
  await page.getByRole("tab", { name: /^Attachments/ }).click();
  await expect(page).toHaveURL(`/ai-agents/${id}?tab=attachments`);
  await page.getByRole("button", { name: "Add link" }).click();
  const link = page.getByRole("dialog", { name: `Add a link to ${t} HR assistant` });
  await link.getByLabel("Web address (required)").fill("https://woodgrove.sharepoint.com/sites/hr/Shared%20Documents/HR-assistant-design.docx");
  await link.getByLabel("Title").fill("Design document (SharePoint)");
  await link.getByLabel("Category").selectOption({ label: "Architecture" });
  await link.getByRole("button", { name: "Add link" }).click();
  await expect(link).toHaveCount(0);
  await page.getByRole("button", { name: "Upload file" }).click();
  const upload = page.getByRole("dialog", { name: `Upload a file to ${t} HR assistant` });
  await upload.getByLabel("File (required)").setInputFiles({ name: "dpia.pdf", mimeType: "application/pdf", buffer: PDF("DPIA") });
  await upload.getByLabel("Title").fill("Data protection impact assessment");
  await upload.getByLabel("Category").selectOption({ label: "Security review" });
  await upload.getByRole("button", { name: "Upload" }).click();
  await expect(upload).toHaveCount(0);
  const attachments = page.getByRole("region", { name: `Attachments of ${t} HR assistant table` });
  await expect(attachments.locator('tbody[data-category="architecture"]')).toContainText("Design document (SharePoint)");
  await expect(attachments.locator('tbody[data-category="security_review"]')).toContainText("Data protection impact assessment");
  await expect(page.getByRole("tab", { name: "Attachments (2)" })).toBeVisible();
  const download = await attachments.getByRole("link", { name: "Download Data protection impact assessment" }).getAttribute("href");
  await expectNoViolations(page, "Agent attachments with a link and a PDF");

  // 3. An edit of the record itself (If-Match), including the Business case.
  await page.getByRole("link", { name: `Edit ${t} HR assistant` }).click();
  await page.getByLabel("Risk classification").selectOption({ label: "Limited" });
  await page.getByLabel("Business case").fill("Answers HR policy questions in Teams and Microsoft 365 Copilot.");
  await page.getByRole("group", { name: "Models" }).getByRole("checkbox", { name: new RegExp(`${t} Labs Lens 1`) }).uncheck();
  const [patch] = await Promise.all([
    page.waitForRequest((r) => r.method() === "PATCH" && r.url().endsWith(`/api/v1/ai-agents/${id}`)),
    page.getByRole("button", { name: "Save" }).click(),
  ]);
  expect(patch.headers()["if-match"]).toMatch(/^"/);
  await expect(page).toHaveURL(`/ai-agents/${id}`);

  // 4. History: every write, each with its diff.
  await page.getByRole("tab", { name: "Audit activity" }).click();
  const activity = page.getByRole("region", { name: "Audit activity table" });
  const expected: [RegExp, string, RegExp][] = [
    [/Record created/, "Record created", new RegExp(`${t} HR assistant`)],
    [/Attachment added.*Design document/, "Attachment added", /Design document \(SharePoint\)/],
    [/Attachment added.*Data protection/, "Attachment added", /Data protection impact assessment/],
    [/Fields changed/, "Fields changed", /limited/i],
  ];
  for (const [rowName, label, inDiff] of expected) {
    const row = activity.getByRole("row", { name: rowName }).first();
    await expect(row).toBeVisible();
    await row.getByRole("button", { name: "Show changes" }).click();
    const diff = page.getByRole("table", { name: new RegExp(`^Changes in ${label}`) }).first();
    await expect(diff).toContainText(inDiff);
    await row.getByRole("button", { name: "Hide changes" }).click().catch(() => undefined);
  }
  const edited = activity.getByRole("row", { name: /Fields changed/ }).first();
  await edited.getByRole("button", { name: "Show changes" }).click();
  const diff = page.getByRole("table", { name: /^Changes in Fields changed/ }).first();
  await expect(diff.getByRole("row", { name: /^Business case/ })).toContainText("Microsoft 365 Copilot");
  await expect(diff.getByRole("row", { name: /^Models/ })).toContainText("Lens 1");
  await expectNoViolations(page, "Agent History with diffs");
  await page.context().close();

  // 5. A Viewer reads it all, writes nothing, and can't download by guessing.
  const viewer = await pageAs(browser, baseURL, VIEWER);
  await viewer.goto(`/ai-agents/${id}?tab=attachments`);
  await expect(viewer.getByRole("link", { name: "Download Data protection impact assessment" })).toBeVisible();
  await expect(viewer.getByRole("button", { name: "Upload file" })).toHaveCount(0);
  await expect(viewer.getByRole("button", { name: "Add link" })).toHaveCount(0);
  await expect(viewer.getByRole("link", { name: /^Edit/ })).toHaveCount(0);
  expect((await viewer.request.get(download!)).status()).toBe(200);
  const csrf = (await viewer.context().cookies()).find((c) => c.name === "csrftoken")?.value ?? "";
  expect((await viewer.request.post(`/api/v1/ai-agents/${id}/attachments`, { headers: { "X-CSRFToken": csrf }, multipart: { file: { name: "x.pdf", mimeType: "application/pdf", buffer: PDF() } } })).status()).toBe(403);
  expect((await viewer.request.patch(`/api/v1/ai-agents/${id}`, { headers: { "X-CSRFToken": csrf }, data: { risk_classification: "high" } })).status()).toBe(403);
  expect((await viewer.request.get("/api/v1/attachments/00000000-0000-4000-8000-000000000001/download")).status()).toBe(404);
  // Another organization's attachment is the same 404 (Contoso's editor uploads one; this Viewer asks for it).
  const contoso = await pageAs(browser, baseURL, "editor@example.test");
  const app = await post(contoso, "applications", { name: `${t} Elsewhere`, lifecycle: "active" });
  const ccsrf = (await contoso.context().cookies()).find((c) => c.name === "csrftoken")?.value ?? "";
  const theirs = await (await contoso.request.post(`/api/v1/applications/${app.id}/attachments`, { headers: { "X-CSRFToken": ccsrf }, multipart: { file: { name: "theirs.pdf", mimeType: "application/pdf", buffer: PDF() } } })).json();
  await contoso.context().close();
  expect((await viewer.request.get(theirs.download_url)).status()).toBe(404);
  await viewer.context().close();
});
