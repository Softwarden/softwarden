/** The organization's limits, its data exports and its deletion (docs/API.md "Signup and the organisation
 *  lifecycle"). Loaded on first use.
 *
 *  - Usage and limits (every role, `/admin/limits`, and a section of Organization settings): a meter per limit from
 *    GET organization/limits with its usage of the maximum written out ("12 of 5,000"), "Unlimited" when there is no
 *    maximum, and a status chip (glyph and text) when it is nearly full or reached. Colour is never the only signal.
 *  - Export all data (Admin): ask for an export (capability org.export), and every export the organization can see with
 *    its purpose, status, size and expiry, and a download link while it is ready. The list polls while one is queued or
 *    being prepared. Operator exports read "Requested by support".
 *  - Delete the organization (Admin, capability org.self_delete): type the slug and the current password (a single
 *    sign-on-only account must have signed in within 15 minutes instead); the grace period and the final export are
 *    explained first. While scheduled: the date, the final export and Cancel the deletion. */
import { forwardRef, useEffect, useRef, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { byteSize, LIMIT_NAME, limitAmount, limitName, limitVerb, Problem, REAUTHENTICATION_REQUIRED, tooManyAttempts, when } from "@/api";
import { EmptyState, ErrorBanner, ErrorSummary, Field, Loading, PageHead, TableRegion } from "@/components/app";
import { orgDate } from "@/components/org-banners";
import { chip, SuccessNote } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { DialogFooter } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Modal } from "@/features/admin/admin";
import { typedApi, useTypedQuery, type Schemas } from "@/lib/openapi";
import { problemText, useInvalidate } from "@/lib/query";
import { useMe, useRefreshMe } from "@/lib/session";

type Limit = Schemas["Limit"];
type OrgExport = Schemas["OrgExport"];

/** A success note that can take focus: when an action replaces the control that started it (Delete becomes Cancel the
 *  deletion, Suspend becomes Unsuspend, a removed row's button), focus moves here instead of being lost. It stays mounted,
 *  so the text is announced as a status either way. */
export const FocusNote = forwardRef<HTMLParagraphElement, { text: string }>(function FocusNote({ text }, ref) {
  return (
    <p ref={ref} tabIndex={-1} role="status" data-testid="focus-note"
      className={text ? "mb-4 flex items-center gap-2 rounded-md border border-success-border bg-success-bg px-3 py-2 text-success-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring" : "sr-only"}>
      {text && <><span aria-hidden="true">✓</span>{text}</>}
    </p>
  );
});

/** A FocusNote's text, and `show(text, focus)`, which moves focus to it once it is rendered. */
export function useFocusNote() {
  const ref = useRef<HTMLParagraphElement>(null);
  const [state, setState] = useState({ text: "", focus: 0 });
  useEffect(() => { if (state.focus) ref.current?.focus(); }, [state.focus]);
  return {
    ref,
    text: state.text,
    show: (text: string, focus = false) => setState((s) => ({ text, focus: focus ? s.focus + 1 : s.focus })),
  };
}

// ---- limits -------------------------------------------------------------------------------------------------------

type MeterState = "unlimited" | "ok" | "near" | "reached" | "rate";
function meterState(l: Limit): MeterState {
  if (l.limit === null) return "unlimited";
  if (l.usage === null) return "rate";
  if (l.usage >= l.limit) return "reached";
  return l.limit > 0 && l.usage / l.limit >= 0.9 ? "near" : "ok";
}

function MeterChip({ state }: { state: MeterState }) {
  if (state === "reached") return <span className={chip({ state: "overdue" })}><span aria-hidden="true">!</span>Limit reached</span>;
  if (state === "near") return <span className={chip({ state: "pending" })}><span aria-hidden="true">◐</span>Nearly full</span>;
  if (state === "ok") return <span className={chip({ state: "confirmed" })}><span aria-hidden="true">✓</span>Room left</span>;
  return null;
}

/** One limit: its name, what it counts, the usage of the maximum in words, a bar (decoration only) and its state. */
function Meter({ l }: { l: Limit }) {
  const state = meterState(l);
  const name = l.name in LIMIT_NAME ? limitName(l.name) : l.label;
  const unit = l.name === "attachment_bytes" ? "" : ` ${l.unit}`;
  const used = l.usage === null ? null : limitAmount(l.name, l.usage);
  // The send budgets count what was sent in a window: "3 of 20 emails sent in the last hour".
  const verb = limitVerb(l.name);
  const text = l.limit === null
    ? (used === null ? "Unlimited" : `${used}${unit} ${verb} · Unlimited`)
    : used === null ? `${limitAmount(l.name, l.limit)}${unit} at most` : `${used} of ${limitAmount(l.name, l.limit)}${unit} ${verb}`;
  const fraction = l.limit && l.usage !== null ? Math.min(1, l.usage / l.limit) : l.limit === 0 ? 1 : 0;
  return (
    <li className="border-b border-border-subtle px-4 py-3 last:border-0" data-testid={`limit-${l.name}`}>
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h3 className="font-semibold">{name}</h3>
        <span className="flex flex-wrap items-center gap-2">
          <span className="tabular">{text}</span>
          <MeterChip state={state} />
        </span>
      </div>
      <p className="text-sm text-muted-foreground">{l.label}{l.overridden && " · Set for this organization by the service operator"}</p>
      {state !== "unlimited" && state !== "rate" && (
        <div aria-hidden="true" className="mt-2 h-2 overflow-hidden rounded-sm border border-border-strong bg-muted">
          <div className={state === "reached" ? "h-full bg-destructive" : state === "near" ? "h-full bg-state-pending-border" : "h-full bg-primary"} style={{ width: `${Math.round(fraction * 100)}%` }} />
        </div>
      )}
    </li>
  );
}

export function LimitsSection({ heading = true }: { heading?: boolean }) {
  const q = useTypedQuery("organization/limits");
  return (
    <section aria-labelledby="limits-heading" className="mt-6 max-w-3xl">
      {heading ? <h2 id="limits-heading" className="mb-1 font-display text-section font-semibold">Usage and limits</h2> : <h2 id="limits-heading" className="sr-only">Limits</h2>}
      <p className="mb-3 text-sm text-muted-foreground">What this organization may hold, and how much it uses now. Updates and deletes are never limited; a create that would pass a limit is refused.</p>
      {q.isError && <ErrorBanner message={problemText(q.error, "Couldn't load the limits.")} onRetry={() => void q.refetch()} />}
      {q.isPending && <Loading what="limits" rows={3} />}
      {q.data && (q.data.limits.length === 0
        ? <EmptyState title="No limits">This organization has no limits to show.</EmptyState>
        : <ul className="rounded-lg border bg-card" aria-label="Limits">{q.data.limits.map((l) => <Meter key={l.name} l={l} />)}</ul>)}
    </section>
  );
}

/** `/admin/limits`: every role. */
export function LimitsPage() {
  const me = useMe();
  return (
    <>
      <PageHead title="Usage and limits" desc={`${me.organization.name}'s limits and what it uses now. The service operator sets them; ask an Admin if you need more room.`} />
      <LimitsSection heading={false} />
    </>
  );
}

// ---- exports ------------------------------------------------------------------------------------------------------

const PURPOSE: Record<string, string> = { export: "Export on request", deletion: "Final export before deletion", backup: "Nightly backup", operator: "Requested by support" };

function ExportStatusChip({ status }: { status: OrgExport["status"] }) {
  if (status === "ready") return <span className={chip({ state: "confirmed" })}><span aria-hidden="true">✓</span>Ready</span>;
  if (status === "running") return <span className={chip({ state: "pending" })}><span aria-hidden="true">◐</span>Being prepared</span>;
  if (status === "queued") return <span className={chip({ state: "unreviewed" })}><span aria-hidden="true">○</span>Queued</span>;
  if (status === "failed") return <span className={chip({ state: "overdue" })}><span aria-hidden="true">!</span>Failed</span>;
  return <span className={chip({ state: "neutral" })}><span aria-hidden="true">⊘</span>Expired</span>;
}

const sizeOf = (n: number | null) => (n === null ? "—" : byteSize(n));
const busy = (e: OrgExport) => e.status === "queued" || e.status === "running";

export function ExportsSection() {
  const me = useMe();
  const canExport = me.capabilities.includes("org.export");
  const invalidate = useInvalidate();
  // Poll while one is queued or being prepared (the worker builds it), every five seconds.
  const q = useTypedQuery("organization/exports", {}, { refetchInterval: (query) => (query.state.data?.results.some(busy) ? 5000 : false) });
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [asking, setAsking] = useState(false);
  async function ask() {
    if (asking) return;
    setAsking(true); setError(""); setNotice("");
    try {
      await typedApi("post", "organization/exports", {});
      setNotice("Export requested. It's being prepared; this list updates on its own, and the download appears when it's ready.");
      void invalidate("organization/exports");
    } catch (e) {
      setError(problemText(e, "Couldn't ask for an export."));
    } finally {
      setAsking(false);
    }
  }
  const rows = q.data?.results;
  return (
    <section aria-labelledby="exports-heading" className="mt-6 max-w-5xl">
      <div className="mb-1 flex flex-wrap items-center justify-between gap-3">
        <h2 id="exports-heading" className="font-display text-section font-semibold">Export all data</h2>
        {canExport && <Button type="button" variant="outline" aria-busy={asking || undefined} onClick={() => void ask()}>{asking ? "Asking…" : "Export all data"}</Button>}
      </div>
      <p className="mb-3 max-w-prose text-sm text-muted-foreground">
        A zip of every record, the members, the audit log and the attachment files, without secrets (credential ciphertexts, token and password hashes).
        It can be downloaded by Admins for 7 days once it's ready.{!canExport && " This organization's edition doesn't include asking for one; exports made by support or before deletion are listed here."}
      </p>
      <SuccessNote text={notice} />
      {error && <ErrorBanner message={error} />}
      {q.isError && <ErrorBanner message={problemText(q.error, "Couldn't load the exports.")} onRetry={() => void q.refetch()} />}
      {q.isPending && <Loading what="exports" rows={2} />}
      {rows && rows.length === 0 && <EmptyState title="No exports yet">{canExport ? "Export all data makes one now. It's ready to download in a few minutes, depending on how much the organization holds." : "None has been made for this organization."}</EmptyState>}
      {rows && rows.length > 0 && (
        <TableRegion label="Exports table">
          <Table>
            <TableCaption className="sr-only">This organization's exports, newest first, with their purpose, status, size and expiry</TableCaption>
            <TableHeader><TableRow>
              <TableHead scope="col">Requested</TableHead><TableHead scope="col">Purpose</TableHead><TableHead scope="col">Status</TableHead>
              <TableHead scope="col">Size</TableHead><TableHead scope="col">Expires</TableHead><TableHead scope="col"><span className="sr-only">Download</span></TableHead>
            </TableRow></TableHeader>
            <TableBody>
              {rows.map((e) => (
                <TableRow key={e.id} className="align-top">
                  <TableCell className="whitespace-normal">
                    <span className="tabular">{when(e.created_at)}</span>
                    <span className="block text-sm text-muted-foreground">{e.purpose === "operator" ? "by the service operator" : e.purpose === "backup" ? "by Softwarden" : e.requested_by ? `by ${e.requested_by.name}` : "by someone no longer here"}</span>
                  </TableCell>
                  <TableCell>{PURPOSE[e.purpose] ?? e.purpose}</TableCell>
                  <TableCell className="whitespace-normal"><ExportStatusChip status={e.status} />{e.status === "failed" && e.error && <span className="mt-1 block text-sm">{e.error}</span>}</TableCell>
                  <TableCell className="tabular">{sizeOf(e.size)}</TableCell>
                  <TableCell className="tabular">{e.expires_at ? orgDate(e.expires_at, me.organization.timezone) : e.purpose === "backup" ? "Kept until newer backups replace it" : "—"}</TableCell>
                  <TableCell className="text-right">
                    {e.download_url ? <a href={e.download_url} className="font-semibold">Download<span className="sr-only"> the export of {when(e.created_at)}</span></a> : <span className="text-muted-foreground">{busy(e) ? "Not ready yet" : "—"}</span>}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableRegion>
      )}
      <p className="mt-2 text-sm text-muted-foreground" aria-live="polite">{rows?.some(busy) ? "Checking for the finished export every few seconds." : ""}</p>
    </section>
  );
}

// ---- deletion -----------------------------------------------------------------------------------------------------

export function DeletionSection() {
  const me = useMe();
  const refreshMe = useRefreshMe();
  const invalidate = useInvalidate();
  const location = useLocation();
  // While the final export is being made, poll it every five seconds, so it turns into the download (or says it failed).
  const q = useTypedQuery("organization/deletion", {}, { refetchInterval: (query) => (query.state.data?.export && busy(query.state.data.export) ? 5000 : false) });
  const [open, setOpen] = useState(false);
  const note = useFocusNote();
  const [error, setError] = useState("");
  const [cancelling, setCancelling] = useState(false);
  const box = useRef<HTMLElement>(null);
  // The deletion banner links here (#delete-organization).
  useEffect(() => { if (q.data && location.hash === "#delete-organization") box.current?.scrollIntoView({ block: "start" }); }, [q.data, location.hash]);
  // The banner's final export comes from `me`: reload it when the export's state changes here.
  const exportState = q.data?.export ? `${q.data.export.id}/${q.data.export.status}` : "";
  // Remembered when the state first arrives, so the first load doesn't reload `me`; only a change afterwards does.
  const seenExport = useRef<string | null>(null);
  useEffect(() => {
    if (!q.data) return;
    if (seenExport.current === null) { seenExport.current = exportState; return; }
    if (exportState !== seenExport.current) { seenExport.current = exportState; refreshMe(); }
  }, [q.data, exportState]); // eslint-disable-line react-hooks/exhaustive-deps
  /** Reload the state before saying so: the control that was pressed is replaced, and focus goes to the note. */
  const changed = async (text: string) => {
    refreshMe();
    await invalidate("organization/deletion", "organization/exports", "organization");
    note.show(text, true);
  };
  async function cancel() {
    if (cancelling) return;
    setCancelling(true); setError(""); note.show("");
    try {
      await typedApi("delete", "organization/deletion", {});
      await changed(`The deletion is canceled. ${me.organization.name} and everything in it stay.`);
    } catch (e) {
      setError(problemText(e, "Couldn't cancel the deletion."));
    } finally {
      setCancelling(false);
    }
  }
  const s = q.data;
  if (s && !s.available && !s.scheduled) return null;
  const tz = me.organization.timezone;
  return (
    <section ref={box} id="delete-organization" aria-labelledby="delete-heading" className="mt-6 max-w-xl scroll-mt-4">
      <Card className="border-error-border"><CardContent className="pt-6">
        <h2 id="delete-heading" className="mb-1 font-display text-section font-semibold">Delete the organization</h2>
        <FocusNote ref={note.ref} text={note.text} />
        {error && <ErrorBanner message={error} />}
        {q.isError && <ErrorBanner message={problemText(q.error, "Couldn't load the deletion state.")} onRetry={() => void q.refetch()} />}
        {q.isPending && <Loading what="the deletion state" />}
        {s && !s.scheduled && (
          <>
            <p className="mb-2 text-sm">Deleting schedules the erasure of every record, file, member's access and the audit log of <strong>{me.organization.name}</strong> in {s.grace_days} {s.grace_days === 1 ? "day" : "days"}.</p>
            <p className="mb-4 text-sm">A final export is made at once so you can download everything first. Any Admin can cancel until the date, and every member sees a banner until then.</p>
            <Button type="button" variant="outline" className="border-error-border" onClick={() => setOpen(true)}>Delete the organization…</Button>
          </>
        )}
        {s?.scheduled && (
          <div data-testid="deletion-scheduled">
            <p className="mb-2"><span aria-hidden="true" className="font-bold">! </span><strong>Scheduled for deletion on <span className="tabular">{orgDate(s.due_at, tz)}</span>.</strong></p>
            <p className="mb-2 text-sm">Asked {s.requested_by ? `by ${s.requested_by.name} ` : ""}on <span className="tabular">{orgDate(s.requested_at, tz)}</span>. On that date every record, file and the audit log are erased, and so are the accounts that belong to no other organization. Backups taken before then hold the data for up to {backupsKept(s.backups_kept_days)} more.</p>
            {s.export && (
              <p className="mb-3 text-sm">Final export: <ExportStatusChip status={s.export.status} />{" "}
                {s.export.download_url ? <a href={s.export.download_url} className="font-semibold">Download the final export</a> : busy(s.export) ? "It can be downloaded here once it's ready." : null}
              </p>
            )}
            <Button type="button" variant="outline" aria-busy={cancelling || undefined} onClick={() => void cancel()}>{cancelling ? "Canceling…" : "Cancel the deletion"}</Button>
          </div>
        )}
      </CardContent></Card>
      <Modal title={`Delete ${me.organization.name}?`} open={open} onClose={() => setOpen(false)} returnFocusFallback={() => note.ref.current}
        description={s ? `Everything in it is erased in ${s.grace_days} ${s.grace_days === 1 ? "day" : "days"}, unless an Admin cancels before then. A final export is made now.` : undefined}>
        {s && <DeleteForm graceDays={s.grace_days} backupDays={s.backups_kept_days} onCancel={() => setOpen(false)} onDone={async (due) => { await changed(`Deletion scheduled for ${orgDate(due, tz)}. The final export is being prepared; download it from here or the banner.`); setOpen(false); }} />}
      </Modal>
    </section>
  );
}

/** "56 days (about 8 weeks)": how long the installation's backups keep a purged organization. */
export function backupsKept(days: number) {
  const weeks = Math.round(days / 7);
  return `${days} ${days === 1 ? "day" : "days"}${days >= 14 ? ` (about ${weeks} weeks)` : ""}`;
}

function DeleteForm({ graceDays, backupDays, onCancel, onDone }: { graceDays: number; backupDays: number; onCancel: () => void; onDone: (due: string | null) => Promise<void> }) {
  const me = useMe();
  const slug = me.organization.slug ?? "";
  const [confirm, setConfirm] = useState("");
  const [password, setPassword] = useState("");
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [banner, setBanner] = useState("");
  const [saving, setSaving] = useState(false);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (saving) return;
    setBanner("");
    if (confirm !== slug) return setErrors({ confirm: [`Type the organization's slug, ${slug}, exactly to confirm.`] });
    setSaving(true);
    try {
      const state = await typedApi("post", "organization/deletion", { body: { confirm, ...(password ? { password } : {}) } });
      await onDone(state.due_at);
    } catch (err) {
      setSaving(false);
      if (err instanceof Problem && err.type === REAUTHENTICATION_REQUIRED) return setErrors({ password: [err.detail] });
      if (err instanceof Problem && Object.keys(err.errors).length) return setErrors(err.errors);
      setErrors({});
      setBanner(err instanceof Problem && err.status === 429 ? tooManyAttempts(err.retryAfter) : problemText(err, "Couldn't schedule the deletion."));
    }
  }
  return (
    <form onSubmit={submit} noValidate>
      <ErrorSummary errors={errors} />
      {banner && <p className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2" role="alert">{banner}</p>}
      <ul className="mb-4 ml-5 list-disc text-sm">
        <li>In {graceDays} {graceDays === 1 ? "day" : "days"} every record, file, member's access and the audit log are erased.</li>
        <li>Members' accounts that belong to no other organization are erased too: their email address, name and password are removed.</li>
        <li data-testid="deletion-backups">Backups of this installation taken before then still hold its data for up to {backupsKept(backupDays)} after the erasure, until those backups are pruned.</li>
        <li>A final export is made now; download it before then from Organization settings or the banner.</li>
        <li>Any Admin can cancel until the date.</li>
      </ul>
      <Field id="confirm" label={`Type ${slug} to confirm (required)`} errors={errors}>
        <Input id="confirm" autoComplete="off" spellCheck={false} value={confirm} onChange={(e) => setConfirm(e.target.value)} />
      </Field>
      <Field id="password" label="Your current password" errors={errors}
        help="Required if you sign in with a password. If you sign in only with single sign-on, leave it blank: you must have signed in within the last 15 minutes, so sign out and in again first if it has been longer.">
        <Input id="password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
      </Field>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Keep the organization</Button>
        <Button type="submit" variant="destructive" aria-busy={saving || undefined}>{saving ? "Scheduling…" : `Delete ${me.organization.name}`}</Button>
      </DialogFooter>
    </form>
  );
}

/** Organization settings' lifecycle sections (Admins): limits, exports and, with org.self_delete, deletion. */
export function OrganizationLifecycle() {
  const me = useMe();
  return (
    <>
      <LimitsSection />
      <p className="mt-2 text-sm"><Link to="/admin/limits">Every member can see these limits</Link> from their account menu.</p>
      <ExportsSection />
      {me.capabilities.includes("org.self_delete") && <DeletionSection />}
    </>
  );
}
