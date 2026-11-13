import { expect, test } from "@playwright/test";
import { pageAs, personId, post, token } from "./helpers";

/** Link, confirmation, assessment and retirement-acknowledgment events read as names in the Audit activity tab and
 *  in the Admin audit log. Synthetic data only. */

test("Link and acknowledgment events are readable on both records; the Admin log names links and assessments", async ({ browser, baseURL }) => {
  const t = token();
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  const portal = await post(editor, "applications", { name: `${t} Portal`, lifecycle: "active" });
  const sync = await post(editor, "components", { name: `${t} Sync`, lifecycle: "active", component_type: "script" });
  const link = await post(editor, "relationships", { type: "depends_on", source: portal.id, target: sync.id, purpose: "Reads the nightly file" });
  await post(editor, `relationships/${link.id}/confirmations`, { confirmed_by: await personId(editor, "Dana Ortiz"), evidence: "Email" });
  const assessment = await post(editor, "assessments", { target: sync.id, change_type: "retirement", title: `${t} Retire Sync` });
  await post(editor, `components/${sync.id}`, { lifecycle: "retired", retirement_ack: true, retirement_ack_reason: `${t} replaced by a vendor feed` }, "PATCH");

  // Both ends show the link's events, in words, on their Audit activity tab (any role).
  const viewer = await pageAs(browser, baseURL, "viewer@example.test");
  // Each is a child row of the record, read from its own end ("Linked to Sync: depends on").
  for (const [path, other, phrase] of [[`applications/${portal.id}`, `${t} Sync`, "depends on"], [`components/${sync.id}`, `${t} Portal`, "is needed by"]]) {
    await viewer.goto(`/${path}`);
    await viewer.getByRole("tab", { name: "Audit activity" }).click();
    const activity = viewer.getByRole("region", { name: "Audit activity table" });
    const added = activity.getByRole("row", { name: /Relationship added/ });
    await expect(added).toContainText(`Linked to ${other}: ${phrase}`);
    await expect(added.getByTestId("child-kind")).toHaveText("Relationship");
    const confirmed = activity.getByRole("row", { name: /Link confirmed/ });
    await expect(confirmed).toContainText(`Link to ${other} (${phrase}) confirmed by Dana Ortiz on`);
    await expect(confirmed.getByTestId("child-kind")).toHaveText("Link confirmation");
  }
  // The retirement reason is part of the Fields changed summary.
  await expect(viewer.getByRole("row", { name: /Fields changed/ })).toContainText(`Retirement acknowledged for 1 active dependent (${t} Portal). Reason: ${t} replaced by a vendor feed`);

  // The Admin log names the link and the assessment instead of leaving the Record column empty, and links to them.
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  await admin.goto("/admin/audit");
  await admin.getByLabel("Event").selectOption({ label: "Relationships" });
  const log = admin.getByRole("region", { name: "Audit log table" });
  const linkRow = log.getByRole("row", { name: /Relationship added/ }).filter({ hasText: `${t} Portal` });
  await expect(linkRow.getByRole("link", { name: `${t} Portal depends on ${t} Sync` })).toHaveAttribute("href", `/applications/${portal.id}?tab=relationships`);
  await admin.getByLabel("Event").selectOption({ label: "Link confirmations" });
  await expect(log.getByRole("link", { name: `${t} Portal depends on ${t} Sync` }).first()).toBeVisible();
  await admin.getByLabel("Event").selectOption({ label: "Assessments" });
  const assessmentRow = log.getByRole("row", { name: /Assessment created/ }).filter({ hasText: `${t} Retire Sync` });
  await expect(assessmentRow.getByRole("link", { name: `${t} Retire Sync` })).toHaveAttribute("href", `/assessments/${assessment.id}`);
  await expect(assessmentRow).toContainText(`Retirement of ${t} Sync, draft`);
});
