import { readFile } from "node:fs/promises";
import { expect, test, type Page } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { pageAs, post, token } from "./helpers";

/** Audit depth: a record's History lists its children's events (attachments, relationships from both
 *  ends, instances, integrations) as the child's kind plus a sentence, "Only this record" (`?children=false`) keeps
 *  its own events; the Admin audit log exports CSV with its filters and, with `audit.export`, JSON lines. */

test.describe.configure({ mode: "serial", timeout: 180_000 });

type Rec = { id: string; name: string };
const said = (page: Page, text: string | RegExp) => expect(page.getByRole("status").filter({ hasText: text }).first()).toBeAttached();

test("History: child events read in words on both ends; Only this record is in the URL", async ({ browser, baseURL }) => {
  const t = token();
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  const portal: Rec = await post(editor, "applications", { name: `${t} Portal`, lifecycle: "active" });
  const sync: Rec = await post(editor, "components", { name: `${t} Sync`, lifecycle: "active", component_type: "script" });
  await post(editor, "relationships", { type: "depends_on", source: portal.id, target: sync.id });
  await post(editor, `applications/${portal.id}/attachments`, { url: "https://example.test/runbook", title: `${t} runbook` });
  await post(editor, `applications/${portal.id}/instances`, { name: `${t} Production`, environment: "production" });
  await post(editor, "integrations", { name: `${t} Feed`, lifecycle: "active", source: portal.id, target: sync.id, protocol: "REST" });

  const viewer = await pageAs(browser, baseURL, "viewer@example.test");
  await viewer.goto(`/applications/${portal.id}?tab=activity`);
  const activity = viewer.getByRole("region", { name: "Audit activity table" });
  const attachment = activity.getByRole("row", { name: /Attachment added/ });
  await expect(attachment.getByTestId("child-kind")).toHaveText("Attachment");
  await expect(attachment).toContainText(`Added attachment '${t} runbook' (link)`);
  const instance = activity.getByRole("row", { name: /Instance added/ });
  await expect(instance.getByTestId("child-kind")).toHaveText("Instance");
  await expect(instance).toContainText(`Added instance '${t} Production'`);
  const link = activity.getByRole("row", { name: /Relationship added/ });
  await expect(link.getByTestId("child-kind")).toHaveText("Relationship");
  await expect(link).toContainText(`Linked to ${t} Sync: depends on`);
  await expect(activity.getByTestId("child-kind").filter({ hasText: "Integration" }).first()).toBeVisible();
  // The record's own events keep their summary and have no badge.
  const created = activity.getByRole("row", { name: /Record created/ }).filter({ hasNotText: "Integration" });
  await expect(created.getByTestId("child-kind")).toHaveCount(0);
  await expectNoViolations(viewer, "History with child events");

  // From the other end the same link reads the other way.
  await viewer.goto(`/components/${sync.id}?tab=activity`);
  const fromSync = viewer.getByRole("region", { name: "Audit activity table" }).getByRole("row", { name: /Relationship added/ });
  await expect(fromSync).toContainText(`Linked to ${t} Portal: is needed by`);
  await expect(fromSync.getByTestId("child-kind")).toHaveText("Relationship");

  // Only this record: own events only, kept in the URL.
  await viewer.goto(`/applications/${portal.id}?tab=activity`);
  const only = viewer.getByLabel("Only this record");
  await expect(only).not.toBeChecked();
  await only.check();
  await expect(viewer).toHaveURL(/tab=activity/);
  await expect(viewer).toHaveURL(/children=false/);
  await expect(activity.getByRole("row", { name: /Record created/ })).toBeVisible();
  await expect(activity.getByTestId("child-kind")).toHaveCount(0);
  await expect(activity.getByRole("row", { name: /Attachment added/ })).toHaveCount(0);
  await expect(only).toHaveAccessibleDescription(/Showing the record's own events/);
  await viewer.reload();
  await expect(viewer.getByLabel("Only this record")).toBeChecked();
  await expect(viewer.getByRole("region", { name: "Audit activity table" }).getByRole("row", { name: /Instance added/ })).toHaveCount(0);
  await expectNoViolations(viewer, "History, only this record");
  await viewer.getByLabel("Only this record").uncheck();
  await expect(viewer).not.toHaveURL(/children=false/);
  await expect(viewer.getByRole("region", { name: "Audit activity table" }).getByRole("row", { name: /Instance added/ })).toBeVisible();

  // Integrations and renewals have the same toggle.
  const feeds: { id: string; name: string }[] = (await (await viewer.request.get(`/api/v1/integrations?q=${t}`)).json()).results;
  await viewer.goto(`/integrations/${feeds[0].id}?tab=activity`);
  await expect(viewer.getByLabel("Only this record")).toBeVisible();
  await expect(viewer.getByRole("region", { name: "Audit activity table" }).getByRole("row", { name: /Record created/ })).toBeVisible();
});

test("History: an empty own history says how to see the related items (intercepted)", async ({ browser, baseURL }) => {
  const t = token();
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  const app: Rec = await post(editor, "applications", { name: `${t} Quiet`, lifecycle: "active" });
  await editor.route(`**/api/v1/applications/${app.id}/audit?children=false`, (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ results: [], next: null }) }));
  await editor.goto(`/applications/${app.id}?tab=activity&children=false`);
  await expect(editor.getByText("No events on the record itself")).toBeVisible();
  await expect(editor.getByText(`Clear "Only this record" to see events on its related items too.`)).toBeVisible();
});

test("Audit log export: CSV with the current filters, the too-many refusal, and JSON lines for a SIEM", async ({ browser, baseURL }) => {
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  await admin.goto("/admin/audit?action=resource&from=2020-01-01");
  await expect(admin.getByRole("region", { name: "Audit log table" })).toBeVisible();
  await expect(admin.getByRole("button", { name: "Export for SIEM (JSON lines)" })).toBeVisible(); // every edition
  const csv = admin.getByRole("button", { name: "Export CSV" });
  await expect(csv).toHaveAccessibleDescription("Exports the events that match the filters below.");
  const [request, download] = await Promise.all([
    admin.waitForRequest((r) => r.url().includes("/api/v1/audit-events/export.csv")),
    admin.waitForEvent("download"),
    csv.click(),
  ]);
  const sent = new URL(request.url()).searchParams;
  expect(sent.get("action")).toBe("resource");
  expect(sent.get("from")).toBe("2020-01-01");
  expect([...sent.keys()].sort()).toEqual(["action", "from"]);
  expect(download.suggestedFilename()).toMatch(/^softwarden-audit-log-\d{4}-\d{2}-\d{2}\.csv$/);
  const body = await readFile((await download.path())!);
  expect([...body.subarray(0, 3)]).toEqual([0xef, 0xbb, 0xbf]);
  expect(body.toString("utf8")).toContain("id,time_utc,time_local,actor_id,actor,action,label");
  await said(admin, /Downloaded softwarden-audit-log-.*\.csv with the current filters\. The export is recorded in the audit log\./);

  // While the file streams in, the status region says so.
  let release: () => void = () => undefined;
  const held = new Promise<void>((done) => { release = done; });
  await admin.route("**/api/v1/audit-events/export.csv*", async (route) => { await held; await route.continue(); });
  const pending = admin.waitForEvent("download");
  await csv.click();
  await expect(admin.getByTestId("audit-export-status")).toHaveText("Preparing the export…");
  await expect(admin.getByRole("button", { name: "Preparing CSV…" })).toBeDisabled();
  release();
  await pending;
  await said(admin, /Downloaded softwarden-audit-log-.*\.csv/);
  await admin.unroute("**/api/v1/audit-events/export.csv*");

  // More than one file holds: the API's count, and a way to narrow the dates.
  await admin.route("**/api/v1/audit-events/export.csv*", (route) => route.fulfill({
    status: 400, contentType: "application/problem+json",
    body: JSON.stringify({ type: "/problems/too-many-events", title: "Too many events to export", status: 400, detail: "123,456 events match; one export holds at most 100,000. Narrow the date range (from, to) or the filters.", errors: { non_field: ["123,456 events match; one export holds at most 100,000. Narrow the date range (from, to) or the filters."] }, count: 123456, limit: 100000 }),
  }));
  await csv.click();
  const refused = admin.getByTestId("audit-export-error");
  await expect(refused).toContainText("Nothing was downloaded. 123,456 events match; one export holds at most 100,000.");
  await expect(refused).toContainText("Set a shorter range with From and To");
  await expectNoViolations(admin, "Audit log with the too-many export message");
  await refused.getByRole("button", { name: "Choose dates" }).click();
  await expect(admin.getByLabel("From")).toBeFocused();
  await admin.unroute("**/api/v1/audit-events/export.csv*");

  // JSON lines for a SIEM (audit.export), in an organization of its own so the export is predictable.
  const { email } = await post(admin, "e2e/enterprise-org", { which: "secondary" });
  const ent = await pageAs(browser, baseURL, email);
  await ent.goto("/admin/audit");
  const siem = ent.getByRole("button", { name: "Export for SIEM (JSON lines)" });
  await expect(siem).toBeVisible();
  await expect(siem).toHaveAccessibleDescription("Exports every event; filter below to export fewer.");
  await expectNoViolations(ent, "Audit log with both exports");
  const [jsonl] = await Promise.all([ent.waitForEvent("download"), siem.click()]);
  expect(jsonl.suggestedFilename()).toMatch(/^softwarden-audit-log-\d{4}-\d{2}-\d{2}\.jsonl$/);
  const first = JSON.parse((await readFile((await jsonl.path())!)).toString("utf8").split("\n")[0]);
  expect(first.schema_version).toBe(1);
  expect(first.organization.slug).toBeTruthy();
  await said(ent, /Downloaded softwarden-audit-log-.*\.jsonl\. The export is recorded/);
});
