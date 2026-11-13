/** A vendor's risk file: the Risk tab of the vendor page. Every role reads it; Editors and Admins change it.
 *
 *  - The profile (GET, PATCH vendors/{id}/risk): tier, personal data, the security questionnaire, the assurance report
 *    (its expiry is worked out by the API and only shown), the DPA, the subprocessor list, notes, and links to the
 *    vendor's own attachments. Edited in place under the ETag of the copy read (If-Match; a 412 says someone else saved
 *    and offers the latest version); only changed fields are sent, and the API's field errors show on their fields.
 *  - The documents (GET, POST vendors/{id}/risk-documents; PATCH, DELETE vendor-risk-documents/{id}): kind, title,
 *    dates and the derived status, a "Replaced" chip on superseded ones (dimmed, hidden with a toggle kept in the URL as
 *    `?replaced=hide`). The expiry is either entered or worked out from a validity in months: a choice, since the API
 *    refuses both together. Delete asks for a checkbox confirmation.
 *  - The gaps, as the ownership gaps' chips. */
import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Problem, type Attachment } from "@/api";
import { EmptyState, ErrorBanner, ErrorSummary, Field, Loading, Select, TableRegion, Textarea, useReturnFocus } from "@/components/app";
import { orgDate } from "@/components/org-banners";
import { DetailList, DetailRow, Panel, SuccessNote } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { hasAttachments } from "@/features/attachments/tab";
import { StaleBanner } from "@/features/money/contract-ui";
import { typedApi, typedGetWithEtag, useTypedQuery, type BodyOf } from "@/lib/openapi";
import { apiKey, problemText, useApiQuery, useInvalidate } from "@/lib/query";
import { canWrite, useMe } from "@/lib/session";
import { cn } from "@/lib/utils";
import { ifMatch, isStaleWrite, staleDeleteText, useOpenedRecord } from "@/lib/if-match";
import {
  ASSURANCE_TYPES, AssuranceStatusChip, assuranceTypeLabel, DATED_KINDS, daysText, DOCUMENT_KINDS, DocumentStatusChip, DPA_STATUSES, dpaLabel,
  kindLabel, QUESTIONNAIRE, questionnaireLabel, ReplacedChip, RiskGaps, TierMark, TIERS,
  type DocumentKind, type RiskDocument, type RiskProfile,
} from "./risk-ui";

type Vendor = { id: string; name: string };
type Errors = Record<string, string[]>;
type RiskAttachmentRef = RiskProfile["assurance_attachment"];
const EDIT_BUTTON = "risk-edit-profile";

const IconShield = <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round"><path d="M8 1.8 13 3.6v3.7c0 3-2 5.4-5 6.3-3-.9-5-3.3-5-6.3V3.6z" /><path d="m5.8 8 1.6 1.6 2.9-3" strokeLinecap="round" /></svg>;
const IconDocs = <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round"><path d="M4 1.5h5.5L12.5 4.5v10h-8.5z" /><path d="M9.5 1.5v3h3M6.5 8h4M6.5 10.5h4" strokeLinecap="round" /></svg>;

/** Everything the risk file changes elsewhere: the vendor's summary, the overview, the register and Needs attention. */
function useRiskChanged() {
  const invalidate = useInvalidate();
  return () => invalidate("vendors", "vendor-risk-documents", "needs-attention", "reminders", "dashboard");
}

/** The vendor's attachments, to link and choose from (only with the attachments capability). */
function useVendorAttachments(vendorId: string) {
  const me = useMe();
  const on = hasAttachments(me);
  const q = useApiQuery<Attachment[]>(`vendors/${vendorId}/attachments`, { enabled: on });
  return { on, list: q.data, error: q.isError, refetch: () => void q.refetch() };
}

/** One of the vendor's attachments: a download for a file, the address for a link, the title alone before the list is in. */
function AttachmentLink({ item, list, vendor }: { item: RiskAttachmentRef; list: Attachment[] | undefined; vendor: Vendor }) {
  if (!item) return <span className="text-muted-foreground">None linked</span>;
  const full = list?.find((a) => a.id === item.id);
  const href = full ? (full.kind === "file" ? full.download_url : full.url) : null;
  if (!href) return <span>{item.title}</span>;
  return (
    <a href={href} target={full?.kind === "link" ? "_blank" : undefined} rel={full?.kind === "link" ? "noreferrer noopener" : undefined} className="underline">
      {item.title}<span className="sr-only"> ({full?.kind === "file" ? `download, an attachment of ${vendor.name}` : "opens in a new tab"})</span>
    </a>
  );
}

export function RiskTab({ vendor }: { vendor: Vendor }) {
  const me = useMe();
  const write = canWrite(me);
  const profilePath = `vendors/${vendor.id}/risk`;
  // Read with its ETag on every visit: the edit sends it back as If-Match, also before the profile exists.
  const profile = useQuery({ queryKey: [...apiKey(profilePath), "etag"], queryFn: () => typedGetWithEtag("vendors/{id}/risk", { path: { id: vendor.id } }), gcTime: 0, staleTime: 0 });
  const documents = useTypedQuery("vendors/{id}/risk-documents", { path: { id: vendor.id } });
  const attachments = useVendorAttachments(vendor.id);
  const [notice, setNotice] = useState("");
  // The copy being edited, fixed when editing starts (and on "Load the latest version"): a background refetch never
  // replaces what someone is typing; a conflicting save is refused by the ETag (412) instead.
  const [session, setSession] = useState<{ round: number; data: RiskProfile; etag: string | null } | null>(null);
  const editing = session !== null;
  const [stale, setStale] = useState(false);
  const changed = useRiskChanged();
  // Focus: into the form's first field when it opens (ProfileForm), back to "Edit risk profile" when it closes.
  const refocus = useRef(false);
  useEffect(() => {
    if (editing || !refocus.current) return;
    refocus.current = false;
    (document.getElementById(EDIT_BUTTON) ?? document.getElementById("risk-profile-h"))?.focus();
  }, [editing]);
  function startEditing() {
    if (!profile.data) return;
    setNotice("");
    setStale(false);
    setSession({ round: 1, data: profile.data.data, etag: profile.data.etag });
  }
  function stopEditing() {
    refocus.current = true;
    setStale(false);
    setSession(null);
  }
  async function loadLatest() {
    const fresh = await profile.refetch();
    if (!fresh.data) return;
    setStale(false);
    setSession((cur) => ({ round: (cur?.round ?? 0) + 1, data: fresh.data.data, etag: fresh.data.etag }));
  }
  const p = profile.data?.data;
  return (
    <div className="grid gap-5" data-testid="vendor-risk">
      <SuccessNote text={notice} />
      {profile.isError && <ErrorBanner message={problemText(profile.error, "Couldn't load the risk file.")} onRetry={() => void profile.refetch()} />}
      {!p && !profile.isError && <Loading what="the risk file" rows={3} />}
      {p && (
        <>
          <section aria-labelledby="risk-gaps-h" className="rounded-lg border bg-card px-4 py-3" data-testid="risk-gaps">
            <h2 id="risk-gaps-h" className="mb-2 font-display text-section font-semibold">Risk gaps</h2>
            {p.gaps.length > 0
              ? <><RiskGaps gaps={p.gaps} /><p className="mt-2 text-sm text-muted-foreground">Listed in Needs attention until they are closed.</p></>
              : <p className="text-success-foreground"><span aria-hidden="true">✓ </span>No risk gaps.{p.risk_tier ? "" : " Set a risk tier to check for assurance."}</p>}
          </section>
          {!write && <p className="text-sm text-muted-foreground" data-testid="risk-read-only">You can read the risk file. Editors and Admins can change it.</p>}
          <Panel id="risk-profile-h" icon={IconShield} title="Risk profile"
            note={editing ? undefined : p.id ? (p.updated_at ? `Last changed ${orgDate(p.updated_at, me.organization.timezone)}` : undefined) : "Not started yet"}
            action={write && !editing ? <Button id={EDIT_BUTTON} size="sm" variant="outline" onClick={startEditing}>Edit risk profile</Button> : undefined}>
            {session && stale && <div className="px-4 pt-4"><StaleBanner what="risk file" onLoad={() => void loadLatest()} /></div>}
            {session
              ? <ProfileForm key={session.round} vendor={vendor} p={session.data} etag={session.etag} attachments={attachments}
                onCancel={stopEditing} onStale={() => setStale(true)}
                onSaved={(text) => { stopEditing(); setNotice(text); void profile.refetch(); void changed(); }} />
              : <ProfileView vendor={vendor} p={p} attachments={attachments.list} />}
          </Panel>
        </>
      )}
      <Documents vendor={vendor} write={write} documents={documents.data} error={documents.isError ? problemText(documents.error, "Couldn't load the risk documents.") : ""}
        reload={() => void documents.refetch()} attachments={attachments}
        onChanged={(text) => { setNotice(text); void documents.refetch(); void profile.refetch(); void changed(); }} />
    </div>
  );
}

function ProfileView({ vendor, p, attachments }: { vendor: Vendor; p: RiskProfile; attachments: Attachment[] | undefined }) {
  return (
    <DetailList>
      <DetailRow label="Risk tier"><TierMark tier={p.risk_tier} /></DetailRow>
      <DetailRow label="Personal data">{p.processes_personal_data ? "Processes personal data for the organization" : "Doesn't process personal data"}</DetailRow>
      <DetailRow label="Security questionnaire">
        {questionnaireLabel(p.questionnaire_status)}
        <span className="block text-sm text-muted-foreground">Requested {p.questionnaire_requested_on ?? "—"} · Reviewed {p.questionnaire_reviewed_on ?? "—"}</span>
      </DetailRow>
      <DetailRow label="Assurance report">
        <span className="inline-flex flex-wrap items-center gap-2">
          {p.assurance_type ? assuranceTypeLabel(p.assurance_type) : <span className="text-muted-foreground">No report recorded</span>}
          {p.assurance_type && <AssuranceStatusChip status={p.assurance_status} />}
          {p.assurance_superseded && <ReplacedChip />}
        </span>
        {p.assurance_type && (
          <span className="block text-sm text-muted-foreground">
            Issued {p.assurance_issued_on ?? "—"} · valid {p.assurance_validity_months} {p.assurance_validity_months === 1 ? "month" : "months"} · expires {p.assurance_expires_on ?? "— (needs an issue date)"}
          </span>
        )}
        {p.assurance_superseded && <span className="block text-sm text-muted-foreground">A later document of the same kind replaces this report, so it raises no reminder.</span>}
      </DetailRow>
      <DetailRow label="Assurance attachment"><AttachmentLink item={p.assurance_attachment} list={attachments} vendor={vendor} /></DetailRow>
      <DetailRow label="DPA">
        {p.dpa_status ? dpaLabel(p.dpa_status) : <span className="text-muted-foreground">Not recorded</span>}
        {p.dpa_status === "yes" && <span className="block text-sm text-muted-foreground">Signed {p.dpa_signed_on ?? "—"}</span>}
      </DetailRow>
      <DetailRow label="DPA attachment"><AttachmentLink item={p.dpa_attachment} list={attachments} vendor={vendor} /></DetailRow>
      <DetailRow label="Subprocessor list">
        {p.subprocessor_url
          ? <a href={p.subprocessor_url} target="_blank" rel="noreferrer noopener" className="underline [overflow-wrap:anywhere]">{p.subprocessor_url}<span className="sr-only"> (opens in a new tab)</span></a>
          : <span className="text-muted-foreground">No address recorded</span>}
        <span className="block text-sm text-muted-foreground">Reviewed {p.subprocessors_reviewed_on ?? "—"}</span>
      </DetailRow>
      <DetailRow label="Subprocessor attachment"><AttachmentLink item={p.subprocessor_attachment} list={attachments} vendor={vendor} /></DetailRow>
      <DetailRow label="Notes">{p.notes ? <span className="whitespace-pre-wrap">{p.notes}</span> : <span className="text-muted-foreground">None</span>}</DetailRow>
    </DetailList>
  );
}

// ---- the profile form ------------------------------------------------------------------------------------------------

type ProfileValues = {
  risk_tier: string; processes_personal_data: boolean; questionnaire_status: string; questionnaire_requested_on: string; questionnaire_reviewed_on: string;
  assurance_type: string; assurance_issued_on: string; assurance_validity_months: string; assurance_attachment: string;
  dpa_status: string; dpa_signed_on: string; dpa_attachment: string; subprocessor_url: string; subprocessors_reviewed_on: string;
  subprocessor_attachment: string; notes: string;
};
const valuesOf = (p: RiskProfile): ProfileValues => ({
  risk_tier: p.risk_tier ?? "", processes_personal_data: p.processes_personal_data, questionnaire_status: p.questionnaire_status,
  questionnaire_requested_on: p.questionnaire_requested_on ?? "", questionnaire_reviewed_on: p.questionnaire_reviewed_on ?? "",
  assurance_type: p.assurance_type ?? "", assurance_issued_on: p.assurance_issued_on ?? "", assurance_validity_months: String(p.assurance_validity_months),
  assurance_attachment: p.assurance_attachment?.id ?? "", dpa_status: p.dpa_status ?? "", dpa_signed_on: p.dpa_signed_on ?? "",
  dpa_attachment: p.dpa_attachment?.id ?? "", subprocessor_url: p.subprocessor_url, subprocessors_reviewed_on: p.subprocessors_reviewed_on ?? "",
  subprocessor_attachment: p.subprocessor_attachment?.id ?? "", notes: p.notes,
});
const pid = (k: string) => `risk-${k}`;

type AttachmentsState = ReturnType<typeof useVendorAttachments>;

function AttachmentSelect({ id, value, onChange, attachments, current }: {
  id: string; value: string; onChange: (v: string) => void; attachments: AttachmentsState; current: RiskAttachmentRef;
}) {
  const options = (attachments.list ?? []).map((a) => ({ id: a.id, title: a.title }));
  if (current && !options.some((o) => o.id === current.id)) options.push({ id: current.id, title: current.title });
  return (
    <Select id={id} value={value} onChange={(e) => onChange(e.target.value)} disabled={!attachments.list && !attachments.error && attachments.on}>
      <option value="">{attachments.on && !attachments.list && !attachments.error ? "Loading attachments…" : "None"}</option>
      {options.map((o) => <option key={o.id} value={o.id}>{o.title}</option>)}
    </Select>
  );
}

function ProfileForm({ vendor, p, etag, attachments, onCancel, onStale, onSaved }: {
  vendor: Vendor; p: RiskProfile; etag: string | null; attachments: AttachmentsState; onCancel: () => void; onStale: () => void; onSaved: (text: string) => void;
}) {
  const was = valuesOf(p);
  const [v, setV] = useState<ProfileValues>(was);
  const [errors, setErrors] = useState<Errors>({});
  const [banner, setBanner] = useState("");
  const [saving, setSaving] = useState(false);
  // Opening the form moves focus to its first field (the button that opened it is gone).
  useEffect(() => { document.getElementById(pid("risk_tier"))?.focus(); }, []);
  const set = <K extends keyof ProfileValues>(k: K) => (value: ProfileValues[K]) => setV((cur) => ({ ...cur, [k]: value }));
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBanner("");
    const local: Errors = {};
    const months = Number(v.assurance_validity_months);
    if (!/^\d+$/.test(v.assurance_validity_months) || months < 1 || months > 120) local.assurance_validity_months = ["Enter a whole number of months from 1 to 120."];
    if (Object.keys(local).length) return setErrors(local);
    // Only what changed: a PATCH that changes nothing writes nothing, and a field someone else changed meanwhile is
    // refused by the ETag rather than overwritten.
    const body: Record<string, unknown> = {};
    const nullable = (k: keyof ProfileValues) => (v[k] === "" ? null : v[k]);
    for (const k of Object.keys(v) as (keyof ProfileValues)[]) {
      if (v[k] === was[k]) continue;
      if (k === "processes_personal_data") body[k] = v[k];
      else if (k === "assurance_validity_months") body[k] = months;
      else if (k === "questionnaire_status" || k === "notes" || k === "subprocessor_url") body[k] = (v[k] as string).trim();
      else body[k] = nullable(k);
    }
    if (!Object.keys(body).length) return onSaved("Nothing changed.");
    setSaving(true);
    try {
      await typedApi("patch", "vendors/{id}/risk", { path: { id: vendor.id }, body: body as BodyOf<"vendors/{id}/risk", "patch">, headers: etag ? { "If-Match": etag } : {} });
      setSaving(false);
      onSaved(p.id ? `Risk file of ${vendor.name} saved.` : `Risk file of ${vendor.name} started.`);
    } catch (err) {
      setSaving(false);
      if (err instanceof Problem && err.status === 412) return onStale();
      if (err instanceof Problem && Object.keys(err.errors).length) return setErrors(err.errors);
      setBanner(`${problemText(err, "Couldn't save the risk file.")} Nothing was saved.`);
    }
  }
  // The API's keys, shown on the fields' element IDs (risk-<field>); a key the form has no field for is named in the
  // summary, so a refusal such as "This field isn't accepted here." always says which field.
  const known = new Set<string>([...Object.keys(was).filter((k) => attachments.on || !k.endsWith("_attachment")), "assurance_expires_on"]);
  const shown = shownErrors(errors, known, pid);
  const dateField = (k: keyof ProfileValues, label: string, help?: string) => (
    <Field id={pid(k)} label={label} errors={shown} help={help}>
      <Input id={pid(k)} type="date" className="w-44" value={v[k] as string} onChange={(e) => set(k)(e.target.value as never)} />
    </Field>
  );
  const attachmentField = (k: "assurance_attachment" | "dpa_attachment" | "subprocessor_attachment", label: string) => attachments.on && (
    <Field id={pid(k)} label={label} errors={shown} help="One of this vendor's attachments.">
      <AttachmentSelect id={pid(k)} value={v[k]} onChange={set(k)} attachments={attachments} current={p[k]} />
    </Field>
  );
  return (
    <form onSubmit={submit} noValidate className="px-4 py-4" aria-label={`Edit the risk profile of ${vendor.name}`}>
      <ErrorSummary errors={shown} focusOnShow="field" />
      {banner && <ErrorBanner message={banner} />}
      {attachments.on && attachments.error && (
        <p className="mb-3 flex flex-wrap items-center gap-2 text-sm text-error-foreground">
          <span><span aria-hidden="true">! </span>Couldn't load this vendor's attachments to choose from.</span>
          <Button type="button" size="sm" variant="outline" onClick={attachments.refetch}>Retry<span className="sr-only"> loading the attachments</span></Button>
        </p>
      )}
      <fieldset disabled={saving} className="m-0 min-w-0 border-0 p-0">
        <div className="grid gap-x-6 md:grid-cols-2">
          <Field id={pid("risk_tier")} label="Risk tier" errors={shown} help="High and critical vendors need a current SOC 2 report or equivalent.">
            <Select id={pid("risk_tier")} value={v.risk_tier} onChange={(e) => set("risk_tier")(e.target.value)}>
              <option value="">Not assessed</option>
              {TIERS.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
            </Select>
          </Field>
          <div className="mb-3 flex items-start gap-2 self-end pb-2">
            <input type="checkbox" id={pid("processes_personal_data")} checked={v.processes_personal_data} onChange={(e) => set("processes_personal_data")(e.target.checked)}
              className="mt-1 h-4 w-4 accent-primary" aria-invalid={shown[pid("processes_personal_data")] ? true : undefined}
              aria-describedby={[`${pid("processes_personal_data")}-help`, shown[pid("processes_personal_data")] && `${pid("processes_personal_data")}-error`].filter(Boolean).join(" ")} />
            <span>
              <Label htmlFor={pid("processes_personal_data")}>Processes personal data</Label>
              <span id={`${pid("processes_personal_data")}-help`} className="block text-sm text-muted-foreground">A signed DPA is then expected.</span>
              {shown[pid("processes_personal_data")] && <span id={`${pid("processes_personal_data")}-error`} className="block text-sm text-error-foreground">{shown[pid("processes_personal_data")].join(" ")}</span>}
            </span>
          </div>
        </div>
        <fieldset className="mt-2 border-t pt-4">
          <legend className="mb-3 font-display text-section font-semibold">Security questionnaire</legend>
          <div className="grid gap-x-6 md:grid-cols-3">
            <Field id={pid("questionnaire_status")} label="Status" errors={shown} help="Requested or Reviewed without a date takes today's.">
              <Select id={pid("questionnaire_status")} value={v.questionnaire_status} onChange={(e) => set("questionnaire_status")(e.target.value)}>
                {QUESTIONNAIRE.map((q) => <option key={q.value} value={q.value}>{q.label}</option>)}
              </Select>
            </Field>
            {dateField("questionnaire_requested_on", "Requested on")}
            {dateField("questionnaire_reviewed_on", "Reviewed on")}
          </div>
        </fieldset>
        <fieldset className="mt-2 border-t pt-4">
          <legend className="mb-3 font-display text-section font-semibold">Assurance report</legend>
          <div className="grid gap-x-6 md:grid-cols-3">
            <Field id={pid("assurance_type")} label="Type" errors={shown}>
              <Select id={pid("assurance_type")} value={v.assurance_type} onChange={(e) => set("assurance_type")(e.target.value)}>
                <option value="">No report</option>
                {ASSURANCE_TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
              </Select>
            </Field>
            {dateField("assurance_issued_on", "Issued on", "The report period's end or the issue date.")}
            <Field id={pid("assurance_validity_months")} label="Valid for (months)" errors={shown} help="1 to 120; 12 by default.">
              <Input id={pid("assurance_validity_months")} inputMode="numeric" className="w-28" value={v.assurance_validity_months}
                onChange={(e) => set("assurance_validity_months")(e.target.value.replace(/[^0-9]/g, ""))} />
            </Field>
          </div>
          {/* Named, so focus landing here on an error (the summary's link) says where it is. */}
          <div className="mb-3 rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-ring" id={pid("assurance_expires_on")} tabIndex={-1} role="group"
            aria-labelledby={`${pid("assurance_expires_on")}-label`} aria-describedby={shown[pid("assurance_expires_on")] ? `${pid("assurance_expires_on")}-error` : undefined}>
            <p className="text-sm"><span className="font-medium" id={`${pid("assurance_expires_on")}-label`}>Expires on (worked out):</span> <span className="tabular">{p.assurance_expires_on ?? "No expiry yet"}</span></p>
            <p className="text-sm text-muted-foreground">The issue date plus the validity, worked out again when you save. It needs both a type and an issue date.</p>
            {shown[pid("assurance_expires_on")] && <p id={`${pid("assurance_expires_on")}-error`} className="text-sm text-error-foreground">{shown[pid("assurance_expires_on")].join(" ")}</p>}
          </div>
          {attachmentField("assurance_attachment", "Report attachment")}
        </fieldset>
        <fieldset className="mt-2 border-t pt-4">
          <legend className="mb-3 font-display text-section font-semibold">Data processing agreement</legend>
          <div className="grid gap-x-6 md:grid-cols-2">
            <Field id={pid("dpa_status")} label="DPA" errors={shown}>
              <Select id={pid("dpa_status")} value={v.dpa_status} onChange={(e) => set("dpa_status")(e.target.value)}>
                <option value="">Not recorded</option>
                {DPA_STATUSES.map((d) => <option key={d.value} value={d.value}>{d.label}</option>)}
              </Select>
            </Field>
            {dateField("dpa_signed_on", "Signed on")}
          </div>
          {attachmentField("dpa_attachment", "DPA attachment")}
        </fieldset>
        <fieldset className="mt-2 border-t pt-4">
          <legend className="mb-3 font-display text-section font-semibold">Subprocessors</legend>
          <div className="grid gap-x-6 md:grid-cols-2">
            <Field id={pid("subprocessor_url")} label="Subprocessor list address" errors={shown} help="The vendor's published list, http or https.">
              <Input id={pid("subprocessor_url")} type="url" inputMode="url" value={v.subprocessor_url} maxLength={2000} onChange={(e) => set("subprocessor_url")(e.target.value)} />
            </Field>
            {dateField("subprocessors_reviewed_on", "Reviewed on")}
          </div>
          {attachmentField("subprocessor_attachment", "Subprocessor attachment")}
        </fieldset>
        <Field id={pid("notes")} label="Notes" errors={shown} help="Optional, up to 5,000 characters.">
          <Textarea id={pid("notes")} value={v.notes} maxLength={5000} onChange={(e) => set("notes")(e.target.value)} />
        </Field>
      </fieldset>
      <div className="flex flex-wrap justify-end gap-2 border-t pt-4">
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        <Button type="submit" disabled={saving}>{saving ? "Saving…" : "Save risk profile"}</Button>
      </div>
    </form>
  );
}

// ---- documents -------------------------------------------------------------------------------------------------------

/** Field errors keyed by element ID (`id(key)`); a key with no field in `known` keeps its own key and names the field
 *  in its message ("foo: This field isn't accepted here."), so the summary says what was refused. */
function shownErrors(errors: Errors, known: Set<string>, id: (k: string) => string): Errors {
  return Object.fromEntries(Object.entries(errors).map(([k, msgs]) => (known.has(k) ? [id(k), msgs] : [id(`unknown-${k}`), msgs.map((m) => `${k}: ${m}`)])));
}

const docName = (d: { kind: DocumentKind; title: string }) => (d.title ? `${kindLabel(d.kind)} (${d.title})` : kindLabel(d.kind));

function Documents({ vendor, write, documents, error, reload, attachments, onChanged }: {
  vendor: Vendor; write: boolean; documents: RiskDocument[] | undefined; error: string; reload: () => void; attachments: AttachmentsState;
  onChanged: (text: string) => void;
}) {
  // Replaced documents show by default on a vendor's own page (they are its history); hiding them is kept in the URL.
  const [params, setParams] = useSearchParams();
  const hide = params.get("replaced") === "hide";
  const setHide = (on: boolean) => setParams((cur) => { const n = new URLSearchParams(cur); if (on) n.set("replaced", "hide"); else n.delete("replaced"); return n; });
  const [dialog, setDialog] = useState<{ mode: "add" } | { mode: "edit"; doc: RiskDocument } | null>(null);
  const [deleting, setDeleting] = useState<RiskDocument | null>(null);
  const replaced = documents?.filter((d) => d.superseded).length ?? 0;
  const shown = documents?.filter((d) => !(hide && d.superseded)) ?? [];
  const add = write && <Button size="sm" variant="outline" onClick={() => setDialog({ mode: "add" })}>Add document</Button>;
  return (
    <Panel id="risk-docs-h" icon={IconDocs} title="Risk documents" count={documents?.length} action={add || undefined}
      note={add ? undefined : "Soonest expiry first"}>
      {error && <div className="px-4 pt-4"><ErrorBanner message={error} onRetry={reload} /></div>}
      {!documents && !error && <div className="px-4 py-3"><Loading what="the risk documents" /></div>}
      {documents && documents.length === 0 && (
        <div className="p-4">
          <EmptyState title="No risk documents yet">
            Dated evidence such as SOC 2 reports, ISO 27001 certificates, penetration tests, questionnaires, DPAs and insurance certificates.
            {write ? " Add one with its expiry date to get reminders before it lapses." : " Editors and Admins can add them."}
          </EmptyState>
        </div>
      )}
      {documents && documents.length > 0 && (
        <>
          <div className="flex flex-wrap items-center gap-3 px-4 py-2 text-sm">
            <span className="flex items-center gap-2">
              <input type="checkbox" id="risk-hide-replaced" checked={hide} onChange={(e) => setHide(e.target.checked)} className="h-4 w-4 accent-primary" />
              <Label htmlFor="risk-hide-replaced" className="font-normal">Hide replaced documents ({replaced})</Label>
            </span>
            <span className="text-muted-foreground">A replaced document has a later one of the same kind; it raises no reminder and isn't counted.</span>
          </div>
          {shown.length === 0
            ? <p className="px-4 pb-4 text-muted-foreground">Every document here is replaced by a later one. Show replaced documents to see them.</p>
            : (
              <TableRegion label="Risk documents table" className="rounded-none border-0 border-t">
                <Table>
                  <TableCaption className="sr-only">Risk documents of {vendor.name}, the soonest expiry first{hide ? ", replaced ones hidden" : ""}</TableCaption>
                  <TableHeader><TableRow>
                    <TableHead scope="col">Document</TableHead><TableHead scope="col">Issued</TableHead><TableHead scope="col">Expires</TableHead>
                    <TableHead scope="col">Status</TableHead><TableHead scope="col">Attachment</TableHead>
                    {write && <TableHead scope="col"><span className="sr-only">Actions</span></TableHead>}
                  </TableRow></TableHeader>
                  <TableBody>
                    {shown.map((d) => (
                      <TableRow key={d.id} data-document={d.id} data-superseded={d.superseded ? "" : undefined} className={cn("align-top", d.superseded && "text-muted-foreground")}>
                        <TableHead scope="row" className={cn("max-w-xs whitespace-normal", d.superseded ? "font-normal text-muted-foreground" : "font-semibold text-foreground")}>
                          {kindLabel(d.kind)}
                          {d.title && <span className="block font-normal">{d.title}</span>}
                          {d.notes && <span className="block whitespace-pre-wrap text-sm font-normal text-muted-foreground">{d.notes}</span>}
                        </TableHead>
                        <TableCell className="tabular">{d.issued_on ?? "—"}</TableCell>
                        <TableCell className="whitespace-normal">
                          {d.expires_on ? <span className="tabular">{d.expires_on}</span> : <span className="text-muted-foreground">No expiry</span>}
                          {d.expires_on && <span className="block text-sm text-muted-foreground">{daysText(d.days_left)}{d.validity_months ? ` · ${d.validity_months} months from issue` : ""}</span>}
                        </TableCell>
                        <TableCell className="whitespace-normal"><span className="inline-flex flex-wrap gap-1.5"><DocumentStatusChip status={d.status} />{d.superseded && <ReplacedChip />}</span></TableCell>
                        <TableCell className="max-w-48 whitespace-normal [overflow-wrap:anywhere]"><AttachmentLink item={d.attachment} list={attachments.list} vendor={vendor} /></TableCell>
                        {write && (
                          <TableCell className="space-x-2 whitespace-nowrap text-right">
                            <Button size="sm" variant="outline" onClick={() => setDialog({ mode: "edit", doc: d })}>Edit<span className="sr-only"> {docName(d)}</span></Button>
                            <Button size="sm" variant="outline" onClick={() => setDeleting(d)}>Delete<span className="sr-only"> {docName(d)}</span></Button>
                          </TableCell>
                        )}
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </TableRegion>
            )}
        </>
      )}
      <DocumentDialog vendor={vendor} state={dialog} attachments={attachments} onClose={() => setDialog(null)}
        onDone={(text) => { setDialog(null); onChanged(text); }} />
      <DeleteDocumentDialog doc={deleting} onClose={() => setDeleting(null)} onDone={(text) => { setDeleting(null); onChanged(text); }} />
    </Panel>
  );
}

type DocState = { mode: "add" } | { mode: "edit"; doc: RiskDocument } | null;

function DocumentDialog({ vendor, state, attachments, onClose, onDone }: {
  vendor: Vendor; state: DocState; attachments: AttachmentsState; onClose: () => void; onDone: (text: string) => void;
}) {
  const open = state !== null;
  const returnFocus = useReturnFocus(open, () => document.getElementById("risk-docs-h"));
  const editing = state?.mode === "edit" ? state.doc : null;
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>{editing ? `Edit ${docName(editing)}` : `Add a risk document to ${vendor.name}`}</DialogTitle>
          <DialogDescription>Dated evidence about the vendor. An expiry date raises reminders before it lapses.</DialogDescription>
        </DialogHeader>
        {open && (editing
          ? <EditDocument vendor={vendor} doc={editing} attachments={attachments} onClose={onClose} onDone={onDone} />
          : <DocumentForm vendor={vendor} doc={null} etag={null} attachments={attachments} onClose={onClose} onDone={onDone} onStale={() => undefined} />)}
      </DialogContent>
    </Dialog>
  );
}

/** Reads the document again with its ETag, so the save sends If-Match; a 412 offers the latest version. */
function EditDocument({ vendor, doc, attachments, onClose, onDone }: {
  vendor: Vendor; doc: RiskDocument; attachments: AttachmentsState; onClose: () => void; onDone: (text: string) => void;
}) {
  const path = `vendor-risk-documents/${doc.id}`;
  const fresh = useQuery({ queryKey: [...apiKey(path), "etag"], queryFn: () => typedGetWithEtag("vendor-risk-documents/{id}", { path: { id: doc.id } }), gcTime: 0, staleTime: Infinity, retry: false });
  const [round, setRound] = useState(0);
  const [stale, setStale] = useState(false);
  if (fresh.isError) return <ErrorBanner message={problemText(fresh.error, "Couldn't load the document.")} onRetry={() => void fresh.refetch()} />;
  if (!fresh.data) return <Loading what="the document" />;
  async function loadLatest() {
    await fresh.refetch();
    setStale(false);
    setRound((n) => n + 1);
  }
  return (
    <>
      {stale && <StaleBanner what="document" onLoad={() => void loadLatest()} />}
      <DocumentForm key={round} vendor={vendor} doc={fresh.data.data} etag={fresh.data.etag} attachments={attachments} onClose={onClose} onDone={onDone} onStale={() => setStale(true)} />
    </>
  );
}

type ExpiryMode = "date" | "validity";
const did = (k: string) => `doc-${k}`;

function DocumentForm({ vendor, doc, etag, attachments, onClose, onDone, onStale }: {
  vendor: Vendor; doc: RiskDocument | null; etag: string | null; attachments: AttachmentsState; onClose: () => void; onDone: (text: string) => void; onStale: () => void;
}) {
  const [kind, setKind] = useState<DocumentKind | "">(doc?.kind ?? "");
  const [title, setTitle] = useState(doc?.title ?? "");
  const [issued, setIssued] = useState(doc?.issued_on ?? "");
  const [mode, setMode] = useState<ExpiryMode>(doc?.validity_months ? "validity" : "date");
  const [expires, setExpires] = useState(doc && !doc.validity_months ? doc.expires_on ?? "" : "");
  const [months, setMonths] = useState(doc?.validity_months ? String(doc.validity_months) : "12");
  const [attachment, setAttachment] = useState(doc?.attachment?.id ?? "");
  const [notes, setNotes] = useState(doc?.notes ?? "");
  const [errors, setErrors] = useState<Errors>({});
  const [banner, setBanner] = useState("");
  const [saving, setSaving] = useState(false);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBanner("");
    const local: Errors = {};
    if (!kind) local.kind = ["Choose what kind of document it is."];
    if (mode === "validity" && (!/^\d+$/.test(months) || Number(months) < 1 || Number(months) > 120)) local.validity_months = ["Enter a whole number of months from 1 to 120."];
    if (Object.keys(local).length) return setErrors(local);
    // The expiry is entered or worked out, never both: the other one is sent as null.
    const all: Record<string, unknown> = {
      kind, title: title.trim(), issued_on: issued || null, notes: notes.trim(),
      ...(mode === "validity" ? { validity_months: Number(months), expires_on: null } : { validity_months: null, expires_on: expires || null }),
      ...(attachments.on ? { attachment: attachment || null } : {}),
    };
    let body = all;
    if (doc) {
      const before: Record<string, unknown> = {
        kind: doc.kind, title: doc.title, issued_on: doc.issued_on, notes: doc.notes, validity_months: doc.validity_months,
        expires_on: doc.validity_months ? null : doc.expires_on, attachment: doc.attachment?.id ?? null,
      };
      body = Object.fromEntries(Object.entries(all).filter(([k, val]) => val !== before[k]));
      // A changed expiry choice sends both halves, so the API never sees a validity beside an old date.
      if ("validity_months" in body || "expires_on" in body) Object.assign(body, { validity_months: all.validity_months, expires_on: all.expires_on });
      if (!Object.keys(body).length) return onClose();
    }
    setSaving(true);
    try {
      if (doc) {
        await typedApi("patch", "vendor-risk-documents/{id}", { path: { id: doc.id }, body: body as BodyOf<"vendor-risk-documents/{id}", "patch">, headers: etag ? { "If-Match": etag } : {} });
      } else {
        await typedApi("post", "vendors/{id}/risk-documents", { path: { id: vendor.id }, body: body as BodyOf<"vendors/{id}/risk-documents", "post"> });
      }
      setSaving(false);
      onDone(doc ? `${docName({ kind: kind as DocumentKind, title: title.trim() })} saved.` : `${docName({ kind: kind as DocumentKind, title: title.trim() })} added.`);
    } catch (err) {
      setSaving(false);
      if (err instanceof Problem && err.status === 412) return onStale();
      if (err instanceof Problem && Object.keys(err.errors).length) return setErrors(err.errors);
      setBanner(`${problemText(err, "Couldn't save the document.")} Nothing was saved.`);
    }
  }
  // The API's keys on the fields' element IDs; an expiry error while the validity is chosen shows on the validity, and a
  // key the form has no field for is named in the summary.
  const moved: Errors = {};
  for (const [k, msgs] of Object.entries(errors)) {
    const key = k === "expires_on" && mode === "validity" ? "validity_months" : k === "validity_months" && mode === "date" ? "expires_on" : k;
    moved[key] = [...(moved[key] ?? []), ...msgs];
  }
  const known = new Set(["kind", "title", "issued_on", mode === "validity" ? "validity_months" : "expires_on", "notes", ...(attachments.on ? ["attachment"] : [])]);
  const shown = shownErrors(moved, known, did);
  const needsDate = kind !== "" && DATED_KINDS.includes(kind);
  return (
    <form onSubmit={submit} noValidate>
      <ErrorSummary errors={shown} focusOnShow="field" />
      {banner && <ErrorBanner message={banner} />}
      <fieldset disabled={saving} className="m-0 min-w-0 border-0 p-0">
        <Field id={did("kind")} label="Kind (required)" errors={shown}>
          <Select id={did("kind")} value={kind} onChange={(e) => setKind(e.target.value as DocumentKind | "")}>
            <option value="">Choose a kind…</option>
            {DOCUMENT_KINDS.map((k) => <option key={k.value} value={k.value}>{k.label}</option>)}
          </Select>
        </Field>
        <Field id={did("title")} label="Title" errors={shown} help="Optional, for example the report period: FY26.">
          <Input id={did("title")} value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} />
        </Field>
        <Field id={did("issued_on")} label={mode === "validity" ? "Issued on (required)" : "Issued on"} errors={shown}>
          <Input id={did("issued_on")} type="date" className="w-44" value={issued} onChange={(e) => setIssued(e.target.value)} />
        </Field>
        <fieldset className="mb-3" aria-describedby={did("expiry-help")}>
          <legend className="mb-1.5 text-sm font-medium">How the expiry is set</legend>
          <p id={did("expiry-help")} className="mb-2 text-sm text-muted-foreground">
            {needsDate ? "A SOC 2 report or ISO 27001 certificate needs an expiry date, or an issue date and a validity." : "Leave the date empty for a document that doesn't expire."}
          </p>
          <div className="flex flex-wrap gap-x-5 gap-y-1">
            {([["date", "Enter the expiry date"], ["validity", "Work it out from a validity in months"]] as const).map(([m, label]) => (
              <span key={m} className="flex min-h-6 items-center gap-2">
                <input type="radio" id={did(`mode-${m}`)} name="doc-expiry-mode" value={m} checked={mode === m} onChange={() => {
                  // Switching a worked-out expiry to an entered one starts from the date it gave.
                  if (m === "date" && !expires && doc?.validity_months && doc.expires_on) setExpires(doc.expires_on);
                  setMode(m);
                }} className="h-4 w-4 accent-primary" />
                <Label htmlFor={did(`mode-${m}`)} className="font-normal">{label}</Label>
              </span>
            ))}
          </div>
        </fieldset>
        {mode === "date"
          ? (
            <Field id={did("expires_on")} label={needsDate ? "Expires on (required)" : "Expires on"} errors={shown} help="The last day it is valid.">
              <Input id={did("expires_on")} type="date" className="w-44" value={expires} onChange={(e) => setExpires(e.target.value)} />
            </Field>
          )
          : (
            <Field id={did("validity_months")} label="Valid for (months)" errors={shown} help="The expiry is the issue date plus this many months, worked out when you save.">
              <Input id={did("validity_months")} inputMode="numeric" className="w-28" value={months} onChange={(e) => setMonths(e.target.value.replace(/[^0-9]/g, ""))} />
            </Field>
          )}
        {attachments.on && (
          <Field id={did("attachment")} label="Attachment" errors={shown} help="One of this vendor's attachments.">
            <AttachmentSelect id={did("attachment")} value={attachment} onChange={setAttachment} attachments={attachments} current={doc?.attachment ?? null} />
          </Field>
        )}
        <Field id={did("notes")} label="Notes" errors={shown} help="Optional, up to 5,000 characters.">
          <Textarea id={did("notes")} value={notes} maxLength={5000} onChange={(e) => setNotes(e.target.value)} />
        </Field>
      </fieldset>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
        <Button type="submit" disabled={saving}>{saving ? "Saving…" : doc ? "Save document" : "Add document"}</Button>
      </DialogFooter>
    </form>
  );
}

function DeleteDocumentDialog({ doc, onClose, onDone }: { doc: RiskDocument | null; onClose: () => void; onDone: (text: string) => void }) {
  const open = doc !== null;
  const returnFocus = useReturnFocus(open, () => document.getElementById("risk-docs-h"));
  const [last, setLast] = useState(doc);
  if (doc && doc !== last) setLast(doc);
  const shown = doc ?? last;
  const [sure, setSure] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const close = () => { setSure(false); setError(""); onClose(); };
  // Read with its ETag as the confirmation opens; the delete sends it as If-Match.
  const opened = useOpenedRecord(doc ? `vendor-risk-documents/${doc.id}` : null);
  async function remove(e: React.FormEvent) {
    e.preventDefault();
    if (!doc) return;
    if (!sure) return setError("Tick the box to confirm the document is removed.");
    setBusy(true);
    try {
      await typedApi("delete", "vendor-risk-documents/{id}", { path: { id: doc.id }, headers: ifMatch(await opened.etag()) });
      setBusy(false);
      setSure(false);
      setError("");
      onDone(`${docName(doc)} deleted.`);
    } catch (err) {
      setBusy(false);
      setError(isStaleWrite(err) ? staleDeleteText("document") : `${problemText(err, "Couldn't delete the document.")} Nothing was deleted.`);
    }
  }
  return (
    <Dialog open={open} onOpenChange={(o) => !o && close()}>
      <DialogContent className="max-w-md" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>Delete {shown ? docName(shown) : "document"}?</DialogTitle>
          <DialogDescription>It leaves the risk file and its reminders end. Its attachment stays with the vendor. This can't be undone.</DialogDescription>
        </DialogHeader>
        <form onSubmit={remove} noValidate>
          {error && <p className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2" role="alert">{error}</p>}
          <div className="mb-3 flex items-start gap-2">
            <input type="checkbox" id="doc-delete-sure" checked={sure} onChange={(e) => setSure(e.target.checked)} className="mt-1 h-4 w-4 accent-primary" />
            <Label htmlFor="doc-delete-sure" className="font-normal">I understand this document is removed from the vendor's risk file.</Label>
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={close}>Cancel</Button>
            <Button type="submit" variant="destructive" disabled={busy}>Delete {shown ? docName(shown) : "document"}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
