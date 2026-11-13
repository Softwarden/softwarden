/** Further organization settings sections, loaded on first use (Admins only):
 *
 *  - Attachments: the effective limits and the space used, and the organization's own limits, which can only be
 *    tighter than the installation's (largest file, allowed types, quota). Blank means the installation's value.
 *  - Data-quality checks: a switch per rule. Rules that are also ownership, AI agent or review gaps are always on (shown
 *    on, disabled, with why); rules a capability or the compliance profile doesn't cover are unavailable.
 *  Each section is its own form (PATCH /organization with only its keys), audited by the API. */
import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { apiWriteWithEtag, type Organization, type QualityRule } from "@/api";
import { attempt, ErrorBanner, ErrorSummary, Field, Loading } from "@/components/app";
import { CheckboxPicker } from "@/components/checkbox-picker";
import { SuccessNote } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { sizeText } from "@/features/attachments/paths";
import { RuleStatusChip } from "@/features/quality/completeness";
import { StaleBanner } from "@/features/money/contract-ui";
import { ifMatch, useOpenedRecord } from "@/lib/if-match";
import { apiKey, problemText, useApiQuery } from "@/lib/query";
import { useMe } from "@/lib/session";

export function OrganizationExtras() {
  const me = useMe();
  // The forms start from the organization read with its ETag, shared (one query key) with admin.tsx OrgForm, never
  // the app's cached copy, so a form shows exactly the revision its save names.
  const org = useOpenedRecord<Organization>("organization");
  // "Load the latest version" starts both forms over from a new read.
  const [round, setRound] = useState(0);
  const loadLatest = async () => { await org.reload(); setRound((n) => n + 1); };
  if (org.error) return <ErrorBanner message={problemText(org.error, "Couldn't load the organization's settings.")} onRetry={() => void org.reload()} />;
  if (!org.data) return <Loading what="attachment and data-quality settings" />;
  return (
    <div className="mt-6 grid max-w-xl grid-cols-1 gap-6">
      {org.data.attachments && me.capabilities.includes("attachments") && <AttachmentLimitsForm key={`${round}-${JSON.stringify(org.data.attachments)}`} org={org.data} onLoadLatest={loadLatest} />}
      {me.capabilities.includes("data_quality") && <QualityRulesForm key={round} org={org.data} onLoadLatest={loadLatest} />}
    </div>
  );
}

/** The settings each form saves, to tell a change someone else made from the page's other forms' saves. */
const limitsOf = (o: Organization) => JSON.stringify([o.attachments?.max_size_mb, o.attachments?.quota_mb, o.attachments?.allowed_extensions]);
const rulesOf = (o: Organization) => JSON.stringify(o.data_quality_disabled_rules ?? []);

/** A save of `body` with If-Match: the organization as last known with its ETag (the opened read, or a save's
 *  answer on this page), sent only while what this form saves is still what it started from (`same`); the answer
 *  becomes the copy the next save starts from. "stale" when someone else changed it. */
async function saveOrganization(opened: ReturnType<typeof useOpenedRecord<Organization>>, start: Organization, same: (o: Organization) => string, body: unknown) {
  const now = await opened.latest();
  if (same(now.data) !== same(start)) return "stale" as const;
  opened.adopt(await apiWriteWithEtag<Organization>("organization", { method: "PATCH", body, headers: ifMatch(now.etag) }));
  return "saved" as const;
}

const orBlank = (v: number | null | undefined) => (v == null ? "" : String(v));

function AttachmentLimitsForm({ org, onLoadLatest }: { org: Organization; onLoadLatest: () => Promise<void> }) {
  const client = useQueryClient();
  const limits = org.attachments!;
  const base = limits.installation;
  // The organization's own value shows when it differs from the installation's; blank otherwise.
  const [size, setSize] = useState(limits.max_size_mb !== base.max_size_mb ? String(limits.max_size_mb) : "");
  const [quota, setQuota] = useState(limits.quota_mb !== base.quota_mb ? orBlank(limits.quota_mb) : "");
  const [types, setTypes] = useState<string[]>(limits.allowed_extensions);
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [banner, setBanner] = useState("");
  const [notice, setNotice] = useState("");
  const opened = useOpenedRecord<Organization>("organization");
  const [stale, setStale] = useState(false);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setNotice("");
    setBanner("");
    const found: Record<string, string[]> = {};
    const whole = (v: string, key: string, top: number | null) => {
      if (!v.trim()) return null;
      const n = Number(v);
      if (!/^\d+$/.test(v.trim()) || n < 1 || (top !== null && n > top)) found[key] = [`Enter a whole number of MB from 1 to ${top ?? "any size"}, or leave it blank for the installation's.`];
      return n;
    };
    const body = {
      attachment_max_size_mb: whole(size, "attachment_max_size_mb", base.max_size_mb),
      attachment_quota_mb: whole(quota, "attachment_quota_mb", base.quota_mb),
      attachment_extensions: types.length === base.allowed_extensions.length ? null : types,
    };
    if (!types.length) found.attachment_extensions = ["Choose at least one file type."];
    if (Object.keys(found).length) return setErrors(found);
    let outcome = "saved" as "stale" | "saved";
    const failed = await attempt(async () => { outcome = await saveOrganization(opened, org, limitsOf, body); });
    // Field errors go under their fields; a 412 offers the latest version; anything else (403, a server error) above.
    if (outcome === "stale" || failed?.status === 412) { setErrors({}); return setStale(true); }
    if (failed && Object.keys(failed.errors).length) return setErrors(failed.errors);
    if (failed) { setErrors({}); return setBanner(`${failed.detail} Nothing was saved.`); }
    setErrors({});
    await client.invalidateQueries({ queryKey: apiKey("organization") });
    setNotice("Attachment limits saved.");
  }
  return (
    <Card id="attachment-limits"><CardContent className="pt-6">
      <form onSubmit={submit} noValidate aria-labelledby="h-attachment-limits">
        <h2 id="h-attachment-limits" className="mb-1 font-display text-section font-semibold">Attachments</h2>
        <p className="mb-3 text-sm text-muted-foreground" data-testid="attachment-usage">
          In effect: files up to {limits.max_size_mb} MB; {limits.allowed_extensions.length} file {limits.allowed_extensions.length === 1 ? "type" : "types"}; {sizeText(limits.used_bytes)} used
          {limits.quota_mb !== null ? ` of a ${limits.quota_mb.toLocaleString()} MB quota` : " (no quota)"}.
          This organization's limits can only be tighter than the installation's.
        </p>
        <SuccessNote text={notice} />
        {stale && <StaleBanner what="organization" onLoad={() => void onLoadLatest()} />}
        {banner && <ErrorBanner message={banner} />}
        <ErrorSummary errors={errors} focusOnShow="field" />
        <Field id="attachment_max_size_mb" label="Largest file (MB)" errors={errors} help={`Blank for the installation's ${base.max_size_mb} MB.`}>
          <Input id="attachment_max_size_mb" inputMode="numeric" value={size} onChange={(e) => setSize(e.target.value)} className="w-32" />
        </Field>
        <CheckboxPicker id="attachment_extensions" legend="Allowed file types" columns filterAt={99} errors={errors.attachment_extensions}
          options={base.allowed_extensions.map((x) => ({ id: x, label: x }))} value={types} onChange={setTypes}
          help="Every type the installation allows is chosen by default. Only types with a content check can ever be allowed." />
        <Field id="attachment_quota_mb" label="Quota for all attachments (MB)" errors={errors}
          help={base.quota_mb !== null ? `Blank for the installation's ${base.quota_mb.toLocaleString()} MB.` : "Blank for no quota."}>
          <Input id="attachment_quota_mb" inputMode="numeric" value={quota} onChange={(e) => setQuota(e.target.value)} className="w-40" />
        </Field>
        <Button type="submit">Save attachment limits</Button>
      </form>
    </CardContent></Card>
  );
}

function QualityRulesForm({ org, onLoadLatest }: { org: Organization; onLoadLatest: () => Promise<void> }) {
  const client = useQueryClient();
  const rules = useApiQuery<QualityRule[]>("data-quality/rules");
  const [off, setOff] = useState<string[]>(org.data_quality_disabled_rules ?? []);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  // The settings this form started from; a save moves it on (If-Match, saveOrganization).
  const [start, setStart] = useState(org);
  const opened = useOpenedRecord<Organization>("organization");
  const [stale, setStale] = useState(false);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setNotice("");
    let outcome = "saved" as "stale" | "saved";
    const failed = await attempt(async () => { outcome = await saveOrganization(opened, start, rulesOf, { data_quality_disabled_rules: off }); });
    if (outcome === "stale" || failed?.status === 412) { setError(""); return setStale(true); }
    if (failed) return setError(Object.values(failed.errors).flat().join(" ") || failed.detail);
    setError("");
    setStart((await opened.latest()).data);
    await Promise.all(["organization", "data-quality", "needs-attention", "applications", "components", "ai-agents"].map((p) => client.invalidateQueries({ queryKey: apiKey(p) })));
    setNotice(off.length ? `Data-quality checks saved: ${off.length} turned off.` : "Data-quality checks saved: every available check is on.");
  }
  return (
    <Card id="data-quality-rules"><CardContent className="pt-6">
      <form onSubmit={submit} noValidate aria-labelledby="h-dq-rules">
        <h2 id="h-dq-rules" className="mb-1 font-display text-section font-semibold">Data-quality checks</h2>
        <p className="mb-3 text-sm text-muted-foreground">Checks that are on count toward each record's completeness and the dashboard, and list failing records in Needs attention.</p>
        <SuccessNote text={notice} />
        {stale && <StaleBanner what="organization" onLoad={() => void onLoadLatest()} />}
        {error && <ErrorBanner message={error} />}
        {rules.isError && <ErrorBanner message={problemText(rules.error, "Couldn't load the checks.")} onRetry={() => void rules.refetch()} />}
        {!rules.data && !rules.isError && <Loading what="checks" />}
        {rules.data && (
          <fieldset className="mb-4">
            <legend className="sr-only">Turn checks on or off</legend>
            <ul className="m-0 list-none divide-y divide-border-subtle rounded-md border p-0">
              {rules.data.map((r) => {
                const locked = !!r.covered_by;
                const unavailable = !locked && r.status === "unavailable" && !off.includes(r.id);
                const on = locked || (!off.includes(r.id) && !unavailable);
                const why = locked ? `Always on: it is also ${r.covered_by === "reviews" ? "a review" : r.covered_by === "agent_gaps" ? "an AI agent" : "an ownership"} gap.`
                  : unavailable ? r.status_reason : r.description;
                return (
                  <li key={r.id} className="flex items-start gap-3 px-3 py-2.5" data-rule={r.id}>
                    <input type="checkbox" id={`rule-${r.id}`} checked={on} disabled={locked || unavailable} aria-describedby={`rule-${r.id}-why`}
                      onChange={(e) => setOff((cur) => (e.target.checked ? cur.filter((x) => x !== r.id) : [...cur, r.id]))} className="mt-0.5" />
                    <span className="min-w-0 flex-1">
                      <label htmlFor={`rule-${r.id}`} className="font-medium">{r.label}</label>
                      <span id={`rule-${r.id}-why`} className="block text-sm text-muted-foreground">{why}</span>
                    </span>
                    {locked ? <span className="text-xs font-semibold text-muted-foreground">Always on</span> : <RuleStatusChip status={r.status} />}
                  </li>
                );
              })}
            </ul>
          </fieldset>
        )}
        <Button type="submit" disabled={!rules.data}>Save data-quality checks</Button>
      </form>
    </CardContent></Card>
  );
}
