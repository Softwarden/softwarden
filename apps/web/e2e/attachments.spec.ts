import { expect, type Page, test } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { PDF, pageAs, post, token } from "./helpers";

/** Attachments. A record's Attachments tab (upload a PDF with progress, add a SharePoint link, edit with
 *  If-Match, remove with the retention note), downloads as attachments (headers checked through the API), the
 *  organization's limits checked before sending, the API's 413 and 429, a Viewer's read-only view and unguessable
 *  attachment IDs, and a vendor's attachments dialog. Runs in the shared Contoso organization with the installation's
 *  limits; the organization-limit form is in data-quality.spec.ts (its own organization). */

async function app(page: Page, name: string) {
  return post(page, "applications", { name, lifecycle: "active" }) as Promise<{ id: string; name: string }>;
}

test("Attachments tab: upload a PDF, add a SharePoint link, edit with If-Match, download as an attachment, remove", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const t = token();
  const rec = await app(page, `${t} Payroll`);
  await page.goto(`/applications/${rec.id}?tab=attachments`);
  await expect(page.getByRole("tab", { name: /^Attachments/ })).toHaveAttribute("aria-selected", "true");
  const panel = page.getByRole("region", { name: /^Attachments/ });
  await expect(panel.getByText("No attachments yet")).toBeVisible();
  await expect(page.getByTestId("attachment-limits")).toContainText("Files up to 25 MB");
  await expectNoViolations(page, "Attachments tab, empty");

  // Upload: title and category, a progress bar while it sends, then the row under its category.
  await panel.getByRole("button", { name: "Upload file" }).click();
  const upload = page.getByRole("dialog", { name: `Upload a file to ${t} Payroll` });
  await upload.getByLabel("File (required)").setInputFiles({ name: "payroll-contract.pdf", mimeType: "application/pdf", buffer: PDF() });
  await upload.getByLabel("Title").fill("Payroll contract 2026");
  await upload.getByLabel("Category").selectOption({ label: "Contract" });
  await expectNoViolations(page, "Upload dialog, filled");
  const [request] = await Promise.all([
    page.waitForRequest((r) => r.method() === "POST" && r.url().endsWith(`/api/v1/applications/${rec.id}/attachments`)),
    upload.getByRole("button", { name: "Upload" }).click(),
  ]);
  expect(request.headers()["content-type"]).toMatch(/^multipart\/form-data/);
  await expect(upload).toHaveCount(0);
  await expect(page.getByRole("status").filter({ hasText: "Payroll contract 2026 uploaded" })).toBeAttached();
  await expect(page.getByRole("button", { name: "Upload file" })).toBeFocused(); // focus back on the opener
  const table = page.getByRole("region", { name: `Attachments of ${t} Payroll table` });
  const contract = table.locator('tbody[data-category="contract"]');
  // The group's own heading cell (scope="rowgroup") is a row header too; the file's row is the one that names it.
  const fileHeader = contract.getByRole("rowheader", { name: /Payroll contract 2026/ });
  await expect(fileHeader).toContainText("Payroll contract 2026");
  await expect(fileHeader).toContainText("payroll-contract.pdf");
  await expect(contract.getByRole("row", { name: /Payroll contract 2026/ })).toContainText(/\d+(\.\d)? (bytes|KB)/);
  await expect(contract.getByRole("row", { name: /Payroll contract 2026/ })).toContainText("Sam Editor");
  await expect(page.getByRole("tab", { name: "Attachments (1)" })).toBeVisible();

  // Add a link: http(s) only, checked before sending.
  await panel.getByRole("button", { name: "Add link" }).click();
  const link = page.getByRole("dialog", { name: `Add a link to ${t} Payroll` });
  await link.getByLabel("Web address (required)").fill("javascript:alert(1)");
  await link.getByRole("button", { name: "Add link" }).click();
  await expect(link.getByLabel("Web address (required)")).toBeFocused(); // the first invalid field
  await expect(link.getByText("Enter a full web address starting with http:// or https://.").first()).toBeVisible();
  await link.getByLabel("Web address (required)").fill("https://contoso.sharepoint.com/sites/it/Shared%20Documents/payroll-runbook.docx");
  await link.getByLabel("Title").fill("Payroll runbook (SharePoint)");
  await link.getByLabel("Category").selectOption({ label: "SOP or runbook" });
  await expectNoViolations(page, "Add link dialog, after an error");
  await link.getByRole("button", { name: "Add link" }).click();
  await expect(link).toHaveCount(0);
  const runbook = table.locator('tbody[data-category="sop_runbook"]').getByRole("row", { name: /Payroll runbook/ });
  await expect(runbook.getByRole("link", { name: /Open link Payroll runbook \(SharePoint\)/ })).toHaveAttribute("href", /^https:\/\/contoso\.sharepoint\.com\//);
  await expect(runbook.getByRole("link", { name: /Open link/ })).toHaveAttribute("rel", /noopener/);
  await expectNoViolations(page, "Attachments tab, a file and a link");

  // Download: a plain link to the download URL; the API sends it as an attachment, never inline.
  const download = contract.getByRole("link", { name: "Download Payroll contract 2026" });
  const href = await download.getAttribute("href");
  expect(href).toMatch(/^\/api\/v1\/attachments\/[0-9a-f-]{36}\/download$/);
  const file = await page.request.get(href!);
  expect(file.status()).toBe(200);
  expect(file.headers()["content-disposition"]).toMatch(/^attachment; filename="payroll-contract\.pdf"/);
  expect(file.headers()["x-content-type-options"]).toBe("nosniff");
  expect(file.headers()["content-security-policy"]).toContain("sandbox");
  expect(file.headers()["cache-control"]).toContain("no-store");
  expect((await file.body()).toString()).toContain("%PDF-1.4");

  // Edit the link with If-Match.
  await runbook.getByRole("button", { name: "Actions for Payroll runbook (SharePoint)" }).click();
  await page.getByRole("menuitem", { name: "Edit…" }).click();
  const edit = page.getByRole("dialog", { name: "Edit Payroll runbook (SharePoint)" });
  await expect(edit.getByLabel("Web address (required)")).toHaveValue(/payroll-runbook\.docx$/);
  await edit.getByLabel("Title (required)").fill("Payroll runbook");
  await edit.getByLabel("Category").selectOption({ label: "Architecture" });
  const [patch] = await Promise.all([
    page.waitForRequest((r) => r.method() === "PATCH" && /\/api\/v1\/attachments\/[0-9a-f-]{36}$/.test(r.url())),
    edit.getByRole("button", { name: "Save changes" }).click(),
  ]);
  expect(patch.headers()["if-match"]).toMatch(/^"/);
  await expect(edit).toHaveCount(0);
  await expect(table.locator('tbody[data-category="architecture"]')).toContainText("Payroll runbook");
  await expect(table.locator('tbody[data-category="sop_runbook"]')).toHaveCount(0);

  // Remove the file: the dialog names the retention window and wants a confirmation.
  await contract.getByRole("button", { name: "Actions for Payroll contract 2026" }).click();
  await page.getByRole("menuitem", { name: "Remove…" }).click();
  const remove = page.getByRole("dialog", { name: "Remove Payroll contract 2026?" });
  await expect(remove).toContainText("kept for the retention window of 7 days"); // GET organization attachments.retention_days
  await remove.getByRole("button", { name: "Remove Payroll contract 2026" }).click();
  await expect(remove.getByRole("alert")).toContainText("Tick the box");
  await remove.getByLabel(/can't be downloaded after this/).check();
  await expectNoViolations(page, "Remove attachment dialog");
  await remove.getByRole("button", { name: "Remove Payroll contract 2026" }).click();
  await expect(remove).toHaveCount(0);
  await expect(table.locator('tbody[data-category="contract"]')).toHaveCount(0);
  expect((await page.request.get(href!)).status()).toBe(404);

  // Every write is in the record's Audit activity.
  await page.getByRole("tab", { name: "Audit activity" }).click();
  const activity = page.getByRole("region", { name: "Audit activity table" });
  await expect(activity.getByRole("row", { name: /Attachment added/ })).toHaveCount(2);
  await expect(activity.getByRole("row", { name: /Attachment changed/ })).toHaveCount(1);
  await expect(activity.getByRole("row", { name: /Attachment removed/ })).toHaveCount(1);
  await page.context().close();
});

test("Upload: closing the dialog mid-upload cancels the request; a 413 with no body (a proxy's) still names the size", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const t = token();
  const rec = await app(page, `${t} Cancel`);
  const path = `**/api/v1/applications/${rec.id}/attachments`;
  await page.goto(`/applications/${rec.id}?tab=attachments`);
  await page.getByRole("button", { name: "Upload file" }).click();
  const dialog = page.getByRole("dialog", { name: `Upload a file to ${t} Cancel` });
  await expect(dialog.getByRole("button", { name: "Upload" })).toBeEnabled(); // enabled once the limits are in

  // A slow upload, then the dialog is closed (Escape): the request is cancelled, nothing is stored.
  const failed: string[] = [];
  page.on("requestfailed", (r) => { if (r.method() === "POST" && r.url().includes(`/applications/${rec.id}/attachments`)) failed.push(r.failure()?.errorText ?? ""); });
  await page.route(path, async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    await new Promise((done) => setTimeout(done, 5000));
    await route.continue().catch(() => undefined);
  });
  await dialog.getByLabel("File (required)").setInputFiles({ name: "slow.pdf", mimeType: "application/pdf", buffer: PDF() });
  await dialog.getByRole("button", { name: "Upload" }).click();
  await expect(dialog.getByRole("button", { name: "Uploading…" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect.poll(() => failed.length).toBeGreaterThan(0);
  expect(failed[0]).toMatch(/ABORTED|cancel/i);
  await page.unroute(path);
  await expect(page.getByRole("button", { name: "Upload file" })).toBeFocused();
  const stored = await (await page.request.get(`/api/v1/applications/${rec.id}/attachments`)).json();
  expect(stored).toEqual([]);

  // A proxy's 413 has no problem body: the size message still shows under the file.
  await page.route(path, (route) => route.request().method() === "POST" ? route.fulfill({ status: 413, body: "" }) : route.continue());
  await page.getByRole("button", { name: "Upload file" }).click();
  await dialog.getByLabel("File (required)").setInputFiles({ name: "notes.pdf", mimeType: "application/pdf", buffer: PDF() });
  await dialog.getByRole("button", { name: "Upload" }).click();
  await expect(dialog.getByLabel("File (required)")).toHaveAccessibleDescription(/larger than this installation accepts\. Choose a smaller file\./);
  await page.unroute(path);
  await page.context().close();
});

test("Upload with the limits unknown: a dropped connection on a large file says it may be too large", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const t = token();
  const rec = await app(page, `${t} Dropped`);
  await page.route("**/api/v1/organization", (route) => route.fulfill({ status: 500, contentType: "application/problem+json", body: JSON.stringify({ detail: "Down." }) }));
  await page.route(`**/api/v1/applications/${rec.id}/attachments`, (route) => route.request().method() === "POST" ? route.abort("connectionreset") : route.continue());
  await page.goto(`/applications/${rec.id}?tab=attachments`);
  await page.getByRole("button", { name: "Upload file" }).click();
  const dialog = page.getByRole("dialog", { name: `Upload a file to ${t} Dropped` });
  await dialog.getByLabel("File (required)").setInputFiles({ name: "scan.pdf", mimeType: "application/pdf", buffer: Buffer.concat([PDF(), Buffer.alloc(6 * 1024 * 1024, 32)]) });
  await dialog.getByRole("button", { name: "Upload" }).click();
  await expect(dialog.getByLabel("File (required)")).toHaveAccessibleDescription(/may be larger than this installation accepts/);
  await page.context().close();
});

test("Upload: the organization's limits are checked before sending; a 413, a 429 and a field error are explained", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const t = token();
  const rec = await app(page, `${t} Limits`);
  await page.goto(`/applications/${rec.id}?tab=attachments`);
  await page.getByRole("button", { name: "Upload file" }).click();
  const dialog = page.getByRole("dialog", { name: `Upload a file to ${t} Limits` });
  const posts: string[] = [];
  page.on("request", (r) => { if (r.method() === "POST" && r.url().includes("/attachments")) posts.push(r.url()); });

  // Larger than the organization's 25 MB: refused in the browser, nothing sent.
  await dialog.getByLabel("File (required)").setInputFiles({ name: "huge.pdf", mimeType: "application/pdf", buffer: Buffer.alloc(26 * 1024 * 1024, 32) });
  await expect(dialog.getByText(/larger than this organization's 25 MB limit/).first()).toBeVisible();
  await dialog.getByRole("button", { name: "Upload" }).click();
  await expect(dialog.getByLabel("File (required)")).toHaveAccessibleDescription(/larger than this organization's 25 MB limit/);
  // A type the organization doesn't allow.
  await dialog.getByLabel("File (required)").setInputFiles({ name: "setup.exe", mimeType: "application/octet-stream", buffer: Buffer.from("MZ") });
  await expect(dialog.getByText(/\.exe files can't be attached here/).first()).toBeVisible();
  await expectNoViolations(page, "Upload dialog with a size error");
  expect(posts).toEqual([]);

  // The installation's request limit answered by the server (mocked: a real one needs a 26 MB body).
  await page.route(`**/api/v1/applications/${rec.id}/attachments`, (route) => route.request().method() === "POST"
    ? route.fulfill({ status: 413, contentType: "application/problem+json", body: JSON.stringify({ type: "about:blank", title: "Content Too Large", status: 413, detail: "The upload is larger than this installation accepts (26 MB)." }) })
    : route.continue());
  await dialog.getByLabel("File (required)").setInputFiles({ name: "notes.pdf", mimeType: "application/pdf", buffer: PDF() });
  await dialog.getByRole("button", { name: "Upload" }).click();
  await expect(dialog.getByText("The upload is larger than this installation accepts (26 MB). Choose a smaller file.").first()).toBeVisible();
  await expect(dialog.getByLabel("File (required)")).toBeFocused();

  // Too many uploads: Retry-After says when.
  await page.unroute(`**/api/v1/applications/${rec.id}/attachments`);
  await page.route(`**/api/v1/applications/${rec.id}/attachments`, (route) => route.request().method() === "POST"
    ? route.fulfill({ status: 429, headers: { "Retry-After": "120" }, contentType: "application/problem+json", body: JSON.stringify({ detail: "Request was throttled." }) })
    : route.continue());
  await dialog.getByRole("button", { name: "Upload" }).click();
  await expect(dialog.getByRole("alert").filter({ hasText: "Too many uploads" })).toContainText("about 2 minutes");
  await page.unroute(`**/api/v1/applications/${rec.id}/attachments`);

  // The API's own content check: a .pdf that isn't one is a field error under the file.
  await dialog.getByLabel("File (required)").setInputFiles({ name: "fake.pdf", mimeType: "application/pdf", buffer: Buffer.from("not really a pdf") });
  await dialog.getByRole("button", { name: "Upload" }).click();
  await expect(dialog.getByLabel("File (required)")).toHaveAccessibleDescription(/isn't a PDF/);
  await expectNoViolations(page, "Upload dialog with the API's field error");
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(page.getByRole("button", { name: "Upload file" })).toBeFocused();
  await page.context().close();
});

test("A Viewer reads and downloads attachments but can't add, change or remove them, and can't download by guessing", async ({ browser, baseURL }) => {
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  const t = token();
  const rec = await app(editor, `${t} Viewer view`);
  const csrf = (await editor.context().cookies()).find((c) => c.name === "csrftoken")?.value ?? "";
  const made = await editor.request.post(`/api/v1/applications/${rec.id}/attachments`, {
    headers: { "X-CSRFToken": csrf }, multipart: { file: { name: "sla.pdf", mimeType: "application/pdf", buffer: PDF("SLA") }, title: "SLA", category: "contract" },
  });
  expect(made.status(), await made.text()).toBe(201);
  const attachment = await made.json();
  await editor.context().close();

  const viewer = await pageAs(browser, baseURL, "viewer@example.test");
  await viewer.goto(`/applications/${rec.id}?tab=attachments`);
  const panel = viewer.getByRole("region", { name: /^Attachments/ });
  await expect(panel.getByRole("rowheader", { name: /SLA/ })).toBeVisible();
  await expect(panel.getByRole("link", { name: "Download SLA" })).toBeVisible();
  await expect(viewer.getByRole("button", { name: "Upload file" })).toHaveCount(0);
  await expect(viewer.getByRole("button", { name: "Add link" })).toHaveCount(0);
  await expect(viewer.getByRole("button", { name: /^Actions/ })).toHaveCount(0);
  await expectNoViolations(viewer, "Attachments tab, Viewer");

  expect((await viewer.request.get(attachment.download_url)).status()).toBe(200);
  const vcsrf = (await viewer.context().cookies()).find((c) => c.name === "csrftoken")?.value ?? "";
  const upload = await viewer.request.post(`/api/v1/applications/${rec.id}/attachments`, {
    headers: { "X-CSRFToken": vcsrf }, multipart: { file: { name: "x.pdf", mimeType: "application/pdf", buffer: PDF() } },
  });
  expect(upload.status()).toBe(403);
  const edit = await viewer.request.patch(`/api/v1/attachments/${attachment.id}`, { headers: { "X-CSRFToken": vcsrf }, data: { title: "Mine now" } });
  expect(edit.status()).toBe(403);
  // A guessed ID is the same 404 as one that doesn't exist (IDs are random UUIDs, role-checked).
  const guess = "00000000-0000-4000-8000-000000000000";
  expect((await viewer.request.get(`/api/v1/attachments/${guess}/download`)).status()).toBe(404);
  expect((await viewer.request.get(`/api/v1/attachments/${guess}`)).status()).toBe(404);
  await viewer.context().close();
});

test("A vendor's attachments open in a dialog named in the URL; focus returns to the row's button", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const t = token();
  const vendor = await post(page, "vendors", { name: `${t} Vendor` });
  await page.goto("/admin/people?tab=vendors");
  const open = page.getByRole("button", { name: `Attachments of ${t} Vendor` });
  await open.click();
  await expect(page).toHaveURL(new RegExp(`attachments=${vendor.id}`));
  const dialog = page.getByRole("dialog", { name: `Attachments of ${t} Vendor` });
  await expect(dialog.getByText("No attachments yet")).toBeVisible();
  await dialog.getByRole("button", { name: "Add link" }).click();
  const link = page.getByRole("dialog", { name: `Add a link to ${t} Vendor` });
  await link.getByLabel("Web address (required)").fill("https://contoso.sharepoint.com/sites/procurement/msa.pdf");
  await link.getByLabel("Title").fill("Master services agreement");
  await link.getByLabel("Category").selectOption({ label: "Contract" });
  await link.getByRole("button", { name: "Add link" }).click();
  await expect(link).toHaveCount(0);
  await expect(dialog.locator('tbody[data-category="contract"]')).toContainText("Master services agreement");
  await expectNoViolations(page, "Vendor attachments dialog");
  await dialog.getByRole("button", { name: "Close" }).first().click();
  await expect(dialog).toHaveCount(0);
  await expect(page).not.toHaveURL(/attachments=/);
  await expect(open).toBeFocused();

  // A shared link opens the dialog directly.
  await page.goto(`/admin/people?tab=vendors&attachments=${vendor.id}`);
  await expect(page.getByRole("dialog", { name: `Attachments of ${t} Vendor` })).toContainText("Master services agreement");
  await page.context().close();
});

test("Editing an attachment someone else changed first: 412, then Load the latest version", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const t = token();
  const rec = await app(page, `${t} Race`);
  const made = await post(page, `applications/${rec.id}/attachments`, { url: "https://example.test/a", title: "First title" });
  await page.goto(`/applications/${rec.id}?tab=attachments`);
  await page.getByRole("button", { name: "Actions for First title" }).click();
  await page.getByRole("menuitem", { name: "Edit…" }).click();
  const edit = page.getByRole("dialog", { name: "Edit First title" });
  await expect(edit.getByLabel("Title (required)")).toHaveValue("First title");
  await post(page, `attachments/${made.id}`, { title: "Changed elsewhere" }, "PATCH");
  await edit.getByLabel("Title (required)").fill("My title");
  await edit.getByRole("button", { name: "Save changes" }).click();
  await expect(edit.getByRole("alert")).toContainText("Someone else changed First title");
  await edit.getByRole("button", { name: "Load the latest version" }).click();
  await expect(edit.getByLabel("Title (required)")).toHaveValue("Changed elsewhere");
  await page.context().close();
});

test("Audit log: attachment events filter by the Attachment target type and link to the record's Attachments tab", async ({ browser, baseURL }) => {
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  const t = token();
  const rec = await app(editor, `${t} Audited`);
  await post(editor, `applications/${rec.id}/attachments`, { url: "https://example.test/audited", title: `${t} audited link` });
  await editor.context().close();
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  await admin.goto("/admin/audit?target_type=Attachment");
  await expect(admin.getByLabel("Target type")).toHaveValue("Attachment");
  const row = admin.getByRole("row", { name: new RegExp(`${t} Audited`) }).first();
  await expect(row).toContainText("Attachment added");
  await expect(row).toContainText("On an attachment");
  await expect(row.getByRole("link", { name: `${t} Audited` })).toHaveAttribute("href", `/applications/${rec.id}`);
  await expectNoViolations(admin, "Audit log filtered to attachments");
  await admin.context().close();
});
