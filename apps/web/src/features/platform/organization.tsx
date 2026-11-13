/** One organization in the platform console: its state, live usage, limits, capabilities, members and the
 *  newest platform events, and every action staff can take on it. Each action asks for a reason (up to 500 characters;
 *  the organization's Admins see it in their audit log) in a dialog whose button names the consequence:
 *  change edition, cancel deletion, suspend or unsuspend, grant or revoke a capability (with an optional expiry),
 *  set or remove a limit override, request an export (only the organization's Admins can download it) and impersonate a
 *  member (your password again, then their session in this browser). */
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, byteSize, sessionChange, LIMIT_NAME, limitAmount, limitName, limitVerb, Problem, REAUTHENTICATION_REQUIRED, when } from "@/api";
import { Breadcrumb, EmptyState, ErrorBanner, ErrorSummary, Field, Loading, PageHead, Select, TableRegion, Textarea } from "@/components/app";
import { chip, DetailList, DetailRow } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { DialogFooter } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Modal } from "@/features/admin/admin";
import { FocusNote, useFocusNote } from "@/features/admin/org-lifecycle";
import { EDITIONS, editionLabel, EventsTable, StateChips } from "@/features/platform/console";
import { codeErrors } from "@/features/platform/second-factor";
import type { Schemas } from "@/lib/openapi";
import { problemText, useApiQuery, useInvalidate } from "@/lib/query";

type Detail = Schemas["PlatformOrganizationDetail"];
type Member = Schemas["PlatformMember"];
type Override = Schemas["CapabilityOverride"];
type LimitOverride = Schemas["LimitOverride"];
type PlatformLimits = Schemas["PlatformLimits"];

/** The limits, in the API's order (LIMIT_NAME lists them with their names). */
const LIMITS = Object.keys(LIMIT_NAME);

type Action =
  | { kind: "edition" } | { kind: "suspend" } | { kind: "unsuspend" } | { kind: "cancel-deletion" } | { kind: "export" }
  | { kind: "grant" } | { kind: "remove-override"; override: Override }
  | { kind: "limit"; limit?: string } | { kind: "remove-limit"; override: LimitOverride }
  | { kind: "impersonate"; member: Member };

export function OrganizationPage({ onStarted }: { onStarted: (impersonationId: string | null) => Promise<void> }) {
  const { id = "" } = useParams();
  const base = `platform/organizations/${id}`;
  const q = useApiQuery<Detail>(base, { retry: false });
  const limits = useApiQuery<PlatformLimits>(`${base}/limits`, { retry: false });
  const invalidate = useInvalidate();
  const [action, setAction] = useState<Action | null>(null);
  const note = useFocusNote();
  /** Reload the organization first, then close the dialog: when the action replaced the control that opened it (Suspend
   *  and Unsuspend, Cancel deletion, a removed row's button), focus goes to the note instead of being lost. */
  const done = async (text: string) => { await invalidate(base); note.show(text); setAction(null); };
  if (q.isError) {
    const missing = q.error instanceof Problem && q.error.status === 404;
    return (
      <>
        <Breadcrumb trail={[{ to: "/platform/organizations", label: "Organizations" }]} current="Not found" />
        <PageHead title={missing ? "Organization not found" : "Couldn't load the organization"} />
        <ErrorBanner message={missing ? "No organization has this ID. It may have been deleted." : problemText(q.error, "Couldn't load the organization.")} onRetry={missing ? undefined : () => void q.refetch()} />
      </>
    );
  }
  if (!q.data) return <Loading what="the organization" rows={4} />;
  const o = q.data;
  const u = o.live_usage.counts;
  const open = (a: Action) => { note.show(""); setAction(a); };
  return (
    <>
      <Breadcrumb trail={[{ to: "/platform/organizations", label: "Organizations" }]} current={o.name} />
      <PageHead title={o.name} desc={<>{o.slug} · {editionLabel(o.edition)} edition · {o.timezone} · created <span className="tabular">{o.created_at.slice(0, 10)}</span></>}>
        <Button type="button" variant="outline" onClick={() => open({ kind: "edition" })}>Change edition</Button>
        {o.suspended_at
          ? <Button type="button" variant="outline" onClick={() => open({ kind: "unsuspend" })}>Unsuspend</Button>
          : <Button type="button" variant="outline" onClick={() => open({ kind: "suspend" })}>Suspend</Button>}
        {o.deletion && <Button type="button" variant="outline" onClick={() => open({ kind: "cancel-deletion" })}>Cancel deletion</Button>}
        <Button type="button" variant="outline" onClick={() => open({ kind: "export" })}>Request an export</Button>
      </PageHead>
      <FocusNote ref={note.ref} text={note.text} />
      <div className="mb-4"><StateChips org={o} /></div>
      {o.suspended_at && (
        <p className="mb-4 max-w-prose rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2"><span aria-hidden="true">⊘ </span>Suspended on <span className="tabular">{when(o.suspended_at)}</span>{o.suspended_reason ? `: ${o.suspended_reason}` : "."} Its members can't use it; nothing is removed.</p>
      )}
      {o.deletion && (
        <p className="mb-4 max-w-prose rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2" data-testid="platform-deletion">
          <span aria-hidden="true">! </span>Scheduled for deletion on <span className="tabular">{when(o.deletion.due_at)}</span>{o.deletion.requested_by ? `, asked by ${o.deletion.requested_by.name}` : ""}.
          {o.deletion.paused && <> <span className="font-semibold">Paused while it is suspended:</span> nothing is purged, and unsuspending gives back the time that was left when it was suspended (at least a day).</>}
          {o.deletion.purge_failures > 0 && (
            // A purge that keeps raising no longer blocks the queue, and after three in a row the schedule fails.
            <span className="mt-1 block" data-testid="purge-failures">
              <span className="font-semibold">The purge failed {o.deletion.purge_failures} {o.deletion.purge_failures === 1 ? "time" : "times"} in a row</span>
              {o.deletion.purge_attempted_at && <>, last tried <span className="tabular">{when(o.deletion.purge_attempted_at)}</span></>}.
              {" "}The worker log has the error{o.deletion.purge_failures >= 3
                ? <>; until it is fixed, the schedule “Delete organizations whose deletion grace period has ended” shows as failing on the <Link to="/platform/system" className="underline underline-offset-2">System page</Link>.</>
                : "; it is tried again on the next run."}
            </span>
          )}
        </p>
      )}

      <div className="grid gap-6 lg:grid-cols-2">
        <section aria-labelledby="usage-heading">
          <h2 id="usage-heading" className="mb-2 font-display text-section font-semibold">Usage, counted now</h2>
          <DetailList className="rounded-lg border bg-card p-4">
            <DetailRow label="Records">{u.records_total.toLocaleString("en-US")}{Object.keys(u.records).length > 0 && <span className="block text-sm text-muted-foreground">{Object.entries(u.records).map(([k, n]) => `${k.replace(/_/g, " ")}: ${n}`).join(", ")}</span>}</DetailRow>
            <DetailRow label="Members">{u.active_members} active of {u.members}</DetailRow>
            <DetailRow label="Connectors">{u.connectors}</DetailRow>
            <DetailRow label="API tokens">{u.api_tokens}</DetailRow>
            <DetailRow label="Attachments">{u.attachments} ({byteSize(u.attachment_bytes)})</DetailRow>
            <DetailRow label="Exports">{u.exports}</DetailRow>
            <DetailRow label="Jobs, last 30 days">{u.jobs_30d}</DetailRow>
            <DetailRow label="Last activity">{o.live_usage.last_activity_at ? when(o.live_usage.last_activity_at) : "None yet"}</DetailRow>
            <DetailRow label="Last member sign-in">{o.live_usage.last_sign_in_at ? when(o.live_usage.last_sign_in_at) : "Nobody has signed in"}</DetailRow>
          </DetailList>
        </section>
        <section aria-labelledby="caps-heading">
          <h2 id="caps-heading" className="mb-2 font-display text-section font-semibold">Capabilities</h2>
          <div className="rounded-lg border bg-card p-4 text-sm">
            <p className="mb-1"><span className="font-semibold">Effective now:</span> {o.capabilities.effective.join(", ") || "none"}</p>
            <p className="text-muted-foreground">The {editionLabel(o.edition)} edition gives {o.capabilities.edition.length}; overrides below add or take away from the next request.</p>
          </div>
        </section>
      </div>

      <Section id="overrides" title="Capability overrides" action={<Button type="button" size="sm" variant="outline" onClick={() => open({ kind: "grant" })}>Add capability override</Button>}>
        {o.capabilities.overrides.length === 0 ? <EmptyState title="No capability overrides">The organization has exactly its edition's capabilities. Add one to grant or revoke a capability, for a time or for good.</EmptyState> : (
          <TableRegion label="Capability overrides table">
            <Table>
              <TableCaption className="sr-only">Capability overrides, with their effect, reason, expiry and whether they apply now</TableCaption>
              <TableHeader><TableRow><TableHead scope="col">Capability</TableHead><TableHead scope="col">Effect</TableHead><TableHead scope="col">Applies</TableHead><TableHead scope="col">Expires</TableHead><TableHead scope="col">Reason</TableHead><TableHead scope="col"><span className="sr-only">Actions</span></TableHead></TableRow></TableHeader>
              <TableBody>
                {o.capabilities.overrides.map((c) => (
                  <TableRow key={c.id} className="align-top">
                    <TableHead scope="row" className="font-mono text-sm">{c.capability}</TableHead>
                    <TableCell>{c.effect === "grant" ? <span className={chip({ state: "confirmed" })}><span aria-hidden="true">+</span>Granted</span> : <span className={chip({ state: "overdue" })}><span aria-hidden="true">−</span>Revoked</span>}</TableCell>
                    <TableCell>{c.live ? <span className={chip({ state: "confirmed" })}><span aria-hidden="true">✓</span>Live</span> : <span className={chip({ state: "unreviewed" })}><span aria-hidden="true">○</span>Expired</span>}</TableCell>
                    <TableCell className="tabular">{c.expires_at ? when(c.expires_at) : "Never"}</TableCell>
                    <TableCell className="min-w-48 whitespace-normal">{c.reason}{c.created_by && <span className="block text-xs text-muted-foreground">by {c.created_by.name}, <span className="tabular">{when(c.created_at)}</span></span>}</TableCell>
                    <TableCell className="text-right"><Button size="sm" variant="outline" onClick={() => open({ kind: "remove-override", override: c })}>Remove<span className="sr-only"> the {c.capability} override</span></Button></TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableRegion>
        )}
      </Section>

      <Section id="limits" title="Limits" action={<Button type="button" size="sm" variant="outline" onClick={() => open({ kind: "limit" })}>Set a limit override</Button>}>
        {limits.isError && <ErrorBanner message={problemText(limits.error, "Couldn't load the limits.")} onRetry={() => void limits.refetch()} />}
        {limits.isPending && <Loading what="limits" rows={2} />}
        {limits.data && <LimitsTable data={limits.data} onSet={(limit) => open({ kind: "limit", limit })} onRemove={(override) => open({ kind: "remove-limit", override })} />}
      </Section>

      <Section id="members" title={`Members (${o.member_count})`}>
        {o.members.length === 0 ? <EmptyState title="No members">Nobody belongs to this organization.</EmptyState> : (
          <TableRegion label="Members table">
            <Table>
              <TableCaption className="sr-only">Members of {o.name}, with their role, state and last sign-in</TableCaption>
              <TableHeader><TableRow><TableHead scope="col">Member</TableHead><TableHead scope="col">Role</TableHead><TableHead scope="col">State</TableHead><TableHead scope="col">Last sign-in</TableHead><TableHead scope="col"><span className="sr-only">Actions</span></TableHead></TableRow></TableHeader>
              <TableBody>
                {o.members.map((m) => (
                  <TableRow key={m.id} className="align-top">
                    <TableHead scope="row" className="whitespace-normal font-semibold">{m.name}<span className="block text-xs font-normal text-muted-foreground">{m.email}</span></TableHead>
                    <TableCell>{m.role.charAt(0).toUpperCase() + m.role.slice(1)}</TableCell>
                    <TableCell>{!m.active ? "Deactivated" : m.platform_staff ? "Active · platform staff" : "Active"}</TableCell>
                    <TableCell className="tabular">{m.last_login ? when(m.last_login) : "Never"}</TableCell>
                    <TableCell className="text-right">
                      {m.active && !m.platform_staff && !o.suspended_at
                        ? <Button size="sm" variant="outline" onClick={() => open({ kind: "impersonate", member: m })}>Impersonate<span className="sr-only"> {m.name}</span></Button>
                        : <span className="text-sm text-muted-foreground">{m.platform_staff ? "Staff can't be impersonated" : !m.active ? "Deactivated accounts can't be impersonated" : "Unsuspend to impersonate"}</span>}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableRegion>
        )}
      </Section>

      <Section id="events" title="Recent platform events" action={<Link to={`/platform/audit?organization=${o.id}`} className="text-sm font-semibold">All of this organization's events</Link>}>
        {o.recent_events.length === 0 ? <EmptyState title="No platform events yet">Every console action on this organization is recorded here and in its own audit log.</EmptyState>
          : <EventsTable events={o.recent_events} caption={`The newest platform events about ${o.name}`} showOrganization={false} />}
      </Section>

      <ActionDialog org={o} action={action} onClose={() => setAction(null)} onDone={done} onLimitsChanged={() => invalidate(`${base}/limits`)} onStarted={onStarted}
        returnFocus={() => note.ref.current} />
    </>
  );
}

function Section({ id, title, action, children }: { id: string; title: string; action?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section aria-labelledby={`${id}-heading`} className="mt-8">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-3">
        <h2 id={`${id}-heading`} className="font-display text-section font-semibold">{title}</h2>
        {action}
      </div>
      {children}
    </section>
  );
}

/** "29 days", "less than a day": from now until `iso`. */
function daysLeft(iso: string) {
  const days = Math.floor((Date.parse(iso) - Date.now()) / 86_400_000);
  return days < 1 ? "less than a day" : `${days} ${days === 1 ? "day" : "days"}`;
}

const amount = (name: string, n: number | null) => (n === null ? "Unlimited" : limitAmount(name, n));

function LimitsTable({ data, onSet, onRemove }: { data: PlatformLimits; onSet: (limit: string) => void; onRemove: (o: LimitOverride) => void }) {
  return (
    <TableRegion label="Limits table">
      <Table>
        <TableCaption className="sr-only">Each limit now, its edition default, the usage counted now and any override</TableCaption>
        <TableHeader><TableRow><TableHead scope="col">Limit</TableHead><TableHead scope="col">Now</TableHead><TableHead scope="col">Edition default</TableHead><TableHead scope="col">Used</TableHead><TableHead scope="col">Override</TableHead><TableHead scope="col"><span className="sr-only">Actions</span></TableHead></TableRow></TableHeader>
        <TableBody>
          {data.limits.map((l) => {
            const override = data.overrides.find((x) => x.limit === l.name);
            return (
              <TableRow key={l.name} className="align-top">
                <TableHead scope="row" className="whitespace-normal font-semibold">{limitName(l.name)}<span className="block text-xs font-normal text-muted-foreground">{l.label}</span></TableHead>
                <TableCell className="tabular">{amount(l.name, l.limit)}</TableCell>
                <TableCell className="tabular">{amount(l.name, l.default)}</TableCell>
                <TableCell className="tabular">{l.usage === null ? "—" : limitAmount(l.name, l.usage)}{l.usage !== null && limitVerb(l.name) !== "used" && <span className="block text-xs text-muted-foreground">{limitVerb(l.name)}</span>}</TableCell>
                <TableCell className="min-w-48 whitespace-normal">{override
                  ? <>{amount(l.name, override.value)}{!override.live && " (expired)"}<span className="block text-xs text-muted-foreground">{override.reason}{override.expires_at ? `; until ${when(override.expires_at)}` : ""}</span></>
                  : <span className="text-muted-foreground">None</span>}</TableCell>
                <TableCell className="text-right">
                  <span className="inline-flex flex-wrap justify-end gap-2">
                    <Button size="sm" variant="outline" onClick={() => onSet(l.name)}>Set<span className="sr-only"> the {limitName(l.name)} limit</span></Button>
                    {override && <Button size="sm" variant="outline" onClick={() => onRemove(override)}>Remove override<span className="sr-only"> of {limitName(l.name)}</span></Button>}
                  </span>
                </TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
    </TableRegion>
  );
}

/** The dialog for the chosen action: its own fields, the reason, and a button naming the consequence. */
function ActionDialog({ org, action, onClose, onDone, onLimitsChanged, onStarted, returnFocus }: {
  org: Detail; action: Action | null; onClose: () => void; onDone: (text: string) => Promise<void>; onLimitsChanged: () => Promise<unknown>; onStarted: (impersonationId: string | null) => Promise<void>;
  returnFocus: () => HTMLElement | null;
}) {
  const titles: Record<Action["kind"], string> = {
    edition: `Change ${org.name}'s edition`, suspend: `Suspend ${org.name}?`, unsuspend: `Unsuspend ${org.name}?`, "cancel-deletion": `Cancel ${org.name}'s deletion?`,
    export: `Request an export of ${org.name}`, grant: "Add a capability override", "remove-override": "Remove the capability override?",
    limit: "Set a limit override", "remove-limit": "Remove the limit override?", impersonate: action?.kind === "impersonate" ? `Impersonate ${action.member.name}` : "",
  };
  const descriptions: Record<Action["kind"], string> = {
    edition: "Its capabilities follow the new edition from the next request, plus any overrides.",
    suspend: "Its members are refused on every request until it is unsuspended, and its jobs wait. Nothing is removed.",
    unsuspend: "Its members can use it again at once.",
    "cancel-deletion": "Nothing is removed; every member's deletion banner goes away.",
    export: "For an organization that asked through support. Only its Admins can download it, from their Organization settings; no console page returns the file.",
    grant: "It replaces any override of the same capability and applies from the next request.",
    "remove-override": "The organization goes back to its edition's setting for this capability.",
    limit: "It replaces any override of the same limit. A value below the current usage removes nothing; creates are refused until usage is under it.",
    "remove-limit": "The edition's default applies again.",
    impersonate: "You act as this member, with their role, in this organization only, in this browser. Credentials, members, tokens, secrets, exports and deletion stay closed, and every change is audited with your name.",
  };
  return (
    <Modal title={action ? titles[action.kind] : ""} description={action ? descriptions[action.kind] : undefined} open={action !== null} onClose={onClose} returnFocusFallback={returnFocus}>
      {action && <ActionForm org={org} action={action} onCancel={onClose} onDone={onDone} onLimitsChanged={onLimitsChanged} onStarted={onStarted} />}
    </Modal>
  );
}

function ActionForm({ org, action, onCancel, onDone, onLimitsChanged, onStarted }: {
  org: Detail; action: Action; onCancel: () => void; onDone: (text: string) => Promise<void>; onLimitsChanged: () => Promise<unknown>; onStarted: (impersonationId: string | null) => Promise<void>;
}) {
  const base = `platform/organizations/${org.id}`;
  const firstLimit = action.kind === "limit" ? (action.limit ?? "records") : "records";
  const [form, setForm] = useState({
    reason: "", edition: org.edition as string, capability: "", effect: "grant", expires_at: "", limit: firstLimit, value: "", unlimited: false, password: "", code: "",
  });
  // With the second factor on, starting an impersonation needs a fresh code too (platform/session says).
  const session = useApiQuery<Schemas["PlatformSession"]>("platform/session", { enabled: action.kind === "impersonate", staleTime: 60_000 });
  const codeRequired = action.kind === "impersonate" && !!session.data?.second_factor.required;
  // Until the session says whether a code is needed, Impersonate waits (a request without one would be refused).
  const sessionPending = action.kind === "impersonate" && session.isPending;
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [banner, setBanner] = useState("");
  const [saving, setSaving] = useState(false);
  // Every capability name, with the editions that include it (GET platform/capabilities), as suggestions; the API
  // refuses an unknown name.
  const capabilities = useApiQuery<Schemas["PlatformCapability"][]>("platform/capabilities", { enabled: action.kind === "grant", staleTime: 5 * 60_000 });
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>) => setForm({ ...form, [k]: e.target.value });
  const expires = form.expires_at ? new Date(form.expires_at).toISOString() : undefined;
  const limitLabel = limitName(form.limit);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (saving || sessionPending) return;
    const found: Record<string, string[]> = {};
    if (!form.reason.trim()) found.reason = ["Give a reason: the organization's Admins see it in their audit log."];
    if (form.reason.length > 500) found.reason = ["Keep the reason to 500 characters or fewer."];
    if (action.kind === "grant" && !form.capability.trim()) found.capability = ["Enter a capability name, such as org.export."];
    if (action.kind === "limit" && !form.unlimited && !/^\d+$/.test(form.value.trim())) found.value = ["Enter a whole number, or choose Unlimited."];
    if (codeRequired && !/^\d{6}$/.test(form.code.replace(/\s+/g, ""))) found.code = ["Enter the 6-digit code your authenticator app shows now."];
    setBanner("");
    if (Object.keys(found).length) return setErrors(found);
    setSaving(true);
    const reason = form.reason.trim();
    try {
      switch (action.kind) {
        case "edition":
          await api(`${base}/edition`, { method: "POST", body: { edition: form.edition, reason } });
          return await onDone(`${org.name} is now on the ${editionLabel(form.edition)} edition.`);
        case "suspend": {
          const after = await api<Detail>(`${base}/suspend`, { method: "POST", body: { reason } });
          return await onDone(`${org.name} is suspended. Its members are refused until you unsuspend it.${after.deletion?.paused ? " Its scheduled deletion is paused." : ""}`);
        }
        case "unsuspend": {
          const after = await api<Detail>(`${base}/unsuspend`, { method: "POST", body: { reason } });
          return await onDone(`${org.name} is no longer suspended.${after.deletion ? ` Its deletion resumes: now due on ${when(after.deletion.due_at)}.` : ""}`);
        }
        case "cancel-deletion":
          await api(`${base}/cancel-deletion`, { method: "POST", body: { reason } });
          return await onDone(`${org.name}'s deletion is canceled.`);
        case "export":
          await api(`${base}/exports`, { method: "POST", body: { reason } });
          return await onDone(`An export of ${org.name} is queued. Its Admins can download it from Organization settings once it's ready; it's listed as requested by support.`);
        case "grant":
          await api(`${base}/capability-overrides`, { method: "POST", body: { capability: form.capability.trim(), effect: form.effect, reason, ...(expires ? { expires_at: expires } : {}) } });
          return await onDone(`${form.capability.trim()} is ${form.effect === "grant" ? "granted to" : "revoked from"} ${org.name}${expires ? ` until ${when(expires)}` : ""}.`);
        case "remove-override":
          await api(`${base}/capability-overrides/${action.override.id}/remove`, { method: "POST", body: { reason } });
          return await onDone(`The ${action.override.capability} override is removed.`);
        case "limit": {
          const value = form.unlimited ? null : Number(form.value.trim());
          await api(`${base}/limits`, { method: "POST", body: { limit: form.limit, value, reason, ...(expires ? { expires_at: expires } : {}) } });
          await onLimitsChanged();
          return await onDone(`${limitLabel} is set to ${amount(form.limit, value)} for ${org.name}.`);
        }
        case "remove-limit":
          await api(`${base}/limits/${action.override.limit}/remove`, { method: "POST", body: { reason } });
          await onLimitsChanged();
          return await onDone(`The ${limitName(action.override.limit)} override is removed; the edition's default applies.`);
        case "impersonate": {
          // Rotates the session key: alone, with no read in flight (api.ts sessionChange).
          const started = await sessionChange(() => api<Schemas["ImpersonationState"]>("platform/impersonation", { method: "POST", exclusive: true, body: { organization_id: org.id, user_id: action.member.id, reason, ...(form.password ? { password: form.password } : {}), ...(codeRequired ? { code: form.code.replace(/\s+/g, "") } : {}) } }));
          // App sets this tab's belief and header to the new impersonation, then reloads `me`.
          await onStarted(started.impersonation?.id ?? null);
          return;
        }
      }
    } catch (err) {
      setSaving(false);
      if (err instanceof Problem && err.type === REAUTHENTICATION_REQUIRED) return setErrors({ password: [err.detail] });
      if (codeRequired && err instanceof Problem && (err.status === 429 || err.errors.code)) return setErrors(codeErrors(err));
      if (err instanceof Problem && Object.keys(err.errors).length) return setErrors(err.errors);
      setErrors({});
      setBanner(err instanceof Problem && err.status === 429 ? "Too many attempts. Wait a minute, then try again." : problemText(err, "That didn't work. Try again."));
    }
  }

  const submitLabel: Record<Action["kind"], string> = {
    edition: "Change edition", suspend: `Suspend ${org.name}`, unsuspend: `Unsuspend ${org.name}`, "cancel-deletion": "Cancel the deletion",
    export: "Request the export", grant: form.effect === "grant" ? "Grant the capability" : "Revoke the capability",
    "remove-override": action.kind === "remove-override" ? `Remove the ${action.override.capability} override` : "",
    limit: `Set ${limitLabel}`, "remove-limit": "Remove the override",
    impersonate: action.kind === "impersonate" ? `Impersonate ${action.member.name}` : "",
  };
  const destructive = action.kind === "suspend" || action.kind === "impersonate";
  return (
    <form onSubmit={submit} noValidate>
      <ErrorSummary errors={errors} />
      {banner && <p className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2" role="alert">{banner}</p>}
      {action.kind === "suspend" && org.deletion && (
        <p className="mb-3 rounded-md border border-l-4 border-state-pending-border bg-state-pending px-3 py-2 text-sm text-state-pending-foreground" data-testid="suspend-deletion">
          <span aria-hidden="true">◐ </span>{org.name} is scheduled for deletion on <span className="tabular">{when(org.deletion.due_at)}</span>, {daysLeft(org.deletion.due_at)} from now.
          Suspending pauses it: nothing is purged while it is suspended, and its Admins can't cancel it. Unsuspending gives back the time that is left now, at least a day.
        </p>
      )}
      {action.kind === "unsuspend" && org.deletion?.paused && (
        <p className="mb-3 rounded-md border border-l-4 border-state-pending-border bg-state-pending px-3 py-2 text-sm text-state-pending-foreground" data-testid="unsuspend-deletion">
          <span aria-hidden="true">◐ </span>Its scheduled deletion resumes: the new date is now plus the time that was left when it was suspended (at least a day), and its Admins can cancel it again.
        </p>
      )}
      {action.kind === "impersonate" && <p className="mb-3 text-sm"><span className="font-semibold">{action.member.name}</span> ({action.member.email}), {action.member.role} in {org.name}. It lasts 30 minutes unless the installation sets otherwise, then ends on its own.</p>}
      {action.kind === "edition" && (
        <Field id="edition" label="Edition (required)" errors={errors}>
          <Select id="edition" value={form.edition} onChange={set("edition")}>{EDITIONS.map((x) => <option key={x.value} value={x.value}>{x.label}{x.value === org.edition ? " (current)" : ""}</option>)}</Select>
        </Field>
      )}
      {action.kind === "grant" && (
        <>
          <Field id="capability" label="Capability (required)" errors={errors} help="A capability name, such as org.export or assistant.">
            <Input id="capability" list="capability-names" autoComplete="off" value={form.capability} onChange={set("capability")} />
          </Field>
          <datalist id="capability-names">{(capabilities.data ?? []).map((c) => <option key={c.name} value={c.name} label={c.gated ? `${c.name} (${c.editions.join(", ")})` : c.name} />)}</datalist>
          {capabilities.isError && <p className="mb-3 text-sm text-muted-foreground">Couldn't load the capability names to suggest; type one, and the API checks it.</p>}
          <fieldset className="mb-3">
            <legend className="mb-1.5 text-sm font-medium">Effect</legend>
            <label className="mr-4 inline-flex items-center gap-2 text-sm"><input type="radio" name="effect" value="grant" checked={form.effect === "grant"} onChange={set("effect")} className="h-4 w-4 accent-primary" />Grant it</label>
            <label className="inline-flex items-center gap-2 text-sm"><input type="radio" name="effect" value="revoke" checked={form.effect === "revoke"} onChange={set("effect")} className="h-4 w-4 accent-primary" />Revoke it</label>
          </fieldset>
        </>
      )}
      {action.kind === "limit" && (
        <>
          <Field id="limit" label="Limit (required)" errors={errors}>
            <Select id="limit" value={form.limit} onChange={set("limit")}>{LIMITS.map((l) => <option key={l} value={l}>{limitName(l)}</option>)}</Select>
          </Field>
          <Field id="value" label={`Maximum${form.limit === "attachment_bytes" ? " in bytes" : ""}`} errors={errors}
            help={form.limit.startsWith("api_rate") ? "Requests per minute, at least 1. To stop an organization, suspend it."
              : form.limit === "attachment_bytes" ? "For example 5368709120 for 5 GB."
                : form.limit.startsWith("invitation_emails") ? "Invitation emails through this installation's mail server in the window; 0 refuses every one. An override also lifts the lower budget a new organization has until a second member is active."
                  : "A whole number; 0 refuses every new one."}>
            <Input id="value" inputMode="numeric" value={form.value} disabled={form.unlimited} onChange={set("value")} />
          </Field>
          <label className="mb-3 flex items-center gap-2 text-sm"><input type="checkbox" checked={form.unlimited} onChange={(e) => setForm({ ...form, unlimited: e.target.checked })} className="h-4 w-4 accent-primary" />Unlimited</label>
        </>
      )}
      {(action.kind === "grant" || action.kind === "limit") && (
        <Field id="expires_at" label="Expires" errors={errors} help="Leave blank for no expiry. Your browser's time zone.">
          <Input id="expires_at" type="datetime-local" value={form.expires_at} onChange={set("expires_at")} className="w-60" />
        </Field>
      )}
      <Field id="reason" label="Reason (required)" errors={errors} help="A sentence; the organization's Admins see it in their audit log. Up to 500 characters.">
        <Textarea id="reason" value={form.reason} onChange={set("reason")} maxLength={500} />
      </Field>
      {action.kind === "impersonate" && (
        <Field id="password" label="Your current password" errors={errors} help="Your own staff password, to confirm it's you; never stored or logged. If you sign in only with single sign-on, leave it blank: you must have signed in within the last 15 minutes.">
          <Input id="password" type="password" autoComplete="current-password" value={form.password} onChange={set("password")} />
        </Field>
      )}
      {codeRequired && (
        <Field id="code" label="Code from your authenticator app (required)" errors={errors} help="The 6-digit code it shows now. Each code works once: if you just signed in with one, wait for the next.">
          <Input id="code" inputMode="numeric" autoComplete="one-time-code" maxLength={8} className="w-40 font-mono tabular" value={form.code} onChange={set("code")} />
        </Field>
      )}
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        <Button type="submit" variant={destructive ? "destructive" : "default"} disabled={sessionPending} aria-busy={saving || sessionPending || undefined}>{saving ? "Working…" : sessionPending ? "Checking…" : submitLabel[action.kind]}</Button>
      </DialogFooter>
    </form>
  );
}
