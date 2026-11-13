/** Saved views and their weekly emails, loaded on first use from the record lists' saved views bar
 *  (curation.tsx `SavedViews`) and the profile page:
 *
 *  - SaveViewDialog: the current filters and columns as a new view, private or (Editors and Admins) shared.
 *  - EditViewDialog: rename, share or make private, with If-Match (412: load the latest version).
 *  - DeleteViewDialog: a shared view disappears for everyone; subscriptions to it say so on their next send day.
 *  - SubscriptionDialog: "Email me weekly" on a weekday, on or off, the send time and timezone, the last send, a test
 *    send now (409 and 429 shown inline) and unsubscribe.
 *  - SubscribersDialog (Admins, shared views): who gets the view weekly; subscribe a member, remove one.
 *  - MySubscriptions (profile): the member's own weekly emails, including those of views since deleted.
 *
 *  The API rules are in apps/api/catalog/saved_filters.py and notify/subscriptions.py. */
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  api, apiWithEtag, Problem, WEEKDAYS, type Preferences, type SubscriptionTestResult, type UserRef, type ViewSubscription,
} from "@/api";
import { ErrorBanner, ErrorSummary, Field, Loading, Select, TableRegion, useReturnFocus } from "@/components/app";
import { chip, GapChip } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import {
  columnsToSave, describeColumns, describeQuery, problemLine, viewPath, type Query, type SavedFilter, type ViewColumns,
} from "@/features/catalog/curation";
import { KIND_PLURAL, type RecordKind } from "@/lib/kinds";
import { apiKey, useApiQuery, useInvalidate } from "@/lib/query";
import { ANY_REVISION, ifMatch, isStaleWrite, useOpenedRecord } from "@/lib/if-match";
import { useMe } from "@/lib/session";

/** Field errors from a saved view write, keyed by the dialog's field IDs; the query allow-list in plain words. */
function viewErrors(e: unknown, fallback: string): Record<string, string[]> {
  if (!(e instanceof Problem)) return { "view-name": [fallback] };
  const out: Record<string, string[]> = {};
  for (const [k, v] of Object.entries(e.errors)) {
    if (k === "name") out["view-name"] = v;
    else if (k === "shared") out["view-shared"] = v;
    else if (k === "query") out["view-query"] = v.map(problemLine);
    else if (k === "columns") out["view-columns"] = v;
    else out["view-name"] = [...(out["view-name"] ?? []), ...v];
  }
  return Object.keys(out).length ? out : { "view-name": [e.detail] };
}

/** What a view keeps: its filters and columns, as a focusable block the error summary can link to. */
function Keeps({ query, columnIds, columns, names, errors }: {
  query: Query; columnIds: string[]; columns: ViewColumns; names?: Record<string, string>; errors: Record<string, string[]>;
}) {
  return (
    <dl className="mb-4 grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 text-sm">
      <dt className="text-muted-foreground">Filters</dt>
      <dd id="view-query" tabIndex={-1} className="min-w-0 outline-none" aria-describedby={errors["view-query"] ? "view-query-error" : undefined}>
        {describeQuery(query, names)}
        {errors["view-query"] && <span id="view-query-error" className="block text-error-foreground">{errors["view-query"].join(" ")}</span>}
      </dd>
      <dt className="text-muted-foreground">Columns</dt>
      <dd id="view-columns" tabIndex={-1} className="min-w-0 outline-none">
        {columnIds.length ? describeColumns(columnIds, columns) : "The list's default columns"}
        {errors["view-columns"] && <span className="block text-error-foreground">{errors["view-columns"].join(" ")}</span>}
      </dd>
    </dl>
  );
}

function StaleNote({ onLoad }: { onLoad: () => void }) {
  return (
    <div className="mb-3 flex flex-wrap items-center gap-3 rounded-md border border-l-4 border-error-border bg-error-bg px-4 py-3" role="alert">
      <strong className="min-w-0 flex-1">Someone else changed this after you opened it, so your changes weren't saved. Load the latest version, then make your change again.</strong>
      <Button type="button" size="sm" variant="outline" onClick={onLoad}>Load the latest version</Button>
    </div>
  );
}

const SHARE_HELP = "Everyone in the organization sees a shared view. Only you, as its owner, or an Admin can change or delete it.";

// ---- Save ---------------------------------------------------------------------------------------------------------------

export function SaveViewDialog({ open, onOpenChange, kind, query, columns, canShare, onSaved, names }: {
  open: boolean; onOpenChange: (o: boolean) => void; kind: RecordKind; query: Query; columns: ViewColumns; canShare: boolean;
  onSaved: (f: SavedFilter) => void; names?: Record<string, string>;
}) {
  const returnFocus = useReturnFocus(open);
  const [name, setName] = useState("");
  const [shared, setShared] = useState(false);
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (open) { setName(""); setShared(false); setErrors({}); } }, [open]);
  const keep = columnsToSave(columns);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return setErrors({ "view-name": ["Enter a name."] });
    setBusy(true);
    try {
      const saved = await api<SavedFilter>("saved-filters", { method: "POST", body: { name: name.trim(), kind, query, columns: keep, shared: canShare && shared } });
      onOpenChange(false);
      onSaved(saved);
    } catch (err) {
      setErrors(viewErrors(err, "Couldn't save the view. Try again."));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>Save as view</DialogTitle>
          <DialogDescription>A saved view keeps the filters and columns, not the results: it shows the {KIND_PLURAL[kind].toLowerCase()} that match when you open it.</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} noValidate>
          <ErrorSummary errors={errors} />
          <Field id="view-name" label="Name (required)" errors={errors}>
            <Input id="view-name" value={name} onChange={(e) => setName(e.target.value)} autoComplete="off" maxLength={100} />
          </Field>
          {canShare ? (
            <div className="mb-3">
              <label className="flex items-center gap-2 text-sm">
                <input id="view-shared" type="checkbox" checked={shared} onChange={(e) => setShared(e.target.checked)} className="h-4 w-4 accent-primary" aria-describedby="view-shared-help" />
                Share with everyone
              </label>
              <p id="view-shared-help" className="mt-1 text-sm text-muted-foreground">{SHARE_HELP}</p>
            </div>
          ) : <p className="mb-3 text-sm text-muted-foreground">Only you see this view. Editors and Admins can share views.</p>}
          <Keeps query={query} columnIds={keep} columns={columns} names={names} errors={errors} />
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" disabled={busy}>{busy ? "Saving…" : "Save view"}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

// ---- Rename or share ----------------------------------------------------------------------------------------------------

export function EditViewDialog({ open, onOpenChange, view, etag, canShare, columns, names, onReload, onSaved }: {
  open: boolean; onOpenChange: (o: boolean) => void; view: SavedFilter; etag: string | null; canShare: boolean; columns: ViewColumns;
  names?: Record<string, string>; onReload: () => void; onSaved: (f: SavedFilter) => void;
}) {
  const returnFocus = useReturnFocus(open);
  const [name, setName] = useState(view.name);
  const [shared, setShared] = useState(view.shared);
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [stale, setStale] = useState(false);
  const [busy, setBusy] = useState(false);
  // A fresh copy (opened again, or the latest version loaded) starts the form again.
  useEffect(() => { if (open) { setName(view.name); setShared(view.shared); setErrors({}); setStale(false); } }, [open, etag]); // eslint-disable-line react-hooks/exhaustive-deps
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return setErrors({ "view-name": ["Enter a name."] });
    const body: Record<string, unknown> = {};
    if (name.trim() !== view.name) body.name = name.trim();
    if (shared !== view.shared) body.shared = shared;
    if (!Object.keys(body).length) { onOpenChange(false); return; }
    setBusy(true);
    setErrors({});
    try {
      const saved = await api<SavedFilter>(`saved-filters/${view.id}`, { method: "PATCH", body, headers: etag ? { "If-Match": etag } : undefined });
      onOpenChange(false);
      onSaved(saved);
    } catch (err) {
      if (err instanceof Problem && err.status === 412) setStale(true);
      else setErrors(viewErrors(err, "Couldn't save the view. Try again."));
    } finally {
      setBusy(false);
    }
  }
  // Sharing is for Editors and Admins; anyone who may change a shared view may make it private.
  const showShare = canShare || view.shared;
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>Rename or share “{view.name}”</DialogTitle>
          <DialogDescription>To change its filters or columns, change them on the list and choose Save changes.</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} noValidate>
          {stale && <StaleNote onLoad={() => { setStale(false); onReload(); }} />}
          <ErrorSummary errors={errors} />
          <Field id="view-name" label="Name (required)" errors={errors}>
            <Input id="view-name" value={name} onChange={(e) => setName(e.target.value)} autoComplete="off" maxLength={100} />
          </Field>
          {showShare ? (
            <div className="mb-3">
              <label className="flex items-center gap-2 text-sm">
                <input id="view-shared" type="checkbox" checked={shared} onChange={(e) => setShared(e.target.checked)} className="h-4 w-4 accent-primary"
                  aria-invalid={errors["view-shared"] ? true : undefined} aria-describedby={errors["view-shared"] ? "view-shared-error view-shared-help" : "view-shared-help"} />
                Share with everyone
              </label>
              <p id="view-shared-help" className="mt-1 text-sm text-muted-foreground">
                {SHARE_HELP} {view.shared ? "Making it private hides it from everyone else, and their weekly emails of it stop." : ""}
              </p>
              {errors["view-shared"] && <p id="view-shared-error" className="text-sm text-error-foreground">{errors["view-shared"].join(" ")}</p>}
            </div>
          ) : <p className="mb-3 text-sm text-muted-foreground">Only you see this view. Editors and Admins can share views.</p>}
          <Keeps query={view.query} columnIds={view.columns ?? []} columns={columns} names={names} errors={errors} />
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" disabled={busy || !etag}>{busy ? "Saving…" : !etag ? "Loading…" : "Save"}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

// ---- Delete -------------------------------------------------------------------------------------------------------------

export function DeleteViewDialog({ open, onOpenChange, view, etag, onDeleted, focusAfterDelete = "saved-filter" }: {
  open: boolean; onOpenChange: (o: boolean) => void; view: SavedFilter; etag: string | null; onDeleted: (f: SavedFilter) => void;
  /** The element focused after a delete (the opener is gone). */
  focusAfterDelete?: string;
}) {
  const returnFocus = useReturnFocus(open);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  // After a delete the button that opened the dialog is gone: focus goes to the saved views selector instead.
  const deleted = useRef(false);
  useEffect(() => { if (open) { setError(""); deleted.current = false; } }, [open]);
  // A view that isn't the open one (one that can't be opened as saved) has no ETag yet: it is read as this opens.
  const opened = useOpenedRecord(open && !etag ? `saved-filters/${view.id}` : null);
  async function remove() {
    setBusy(true);
    try {
      await api(`saved-filters/${view.id}`, { method: "DELETE", headers: ifMatch(etag ?? await opened.etag()) });
      deleted.current = true;
      onOpenChange(false);
      onDeleted(view);
    } catch (e) {
      setError(e instanceof Problem && e.status === 412
        ? "Someone else changed this view after you opened it, so it wasn't deleted. Close this, open the view again and check it first."
        : e instanceof Problem ? e.detail : "Couldn't delete the view. Try again.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md" onCloseAutoFocus={(e) => {
        const selector = deleted.current ? document.getElementById(focusAfterDelete) : null;
        if (!selector) return returnFocus.onCloseAutoFocus(e);
        e.preventDefault();
        selector.focus();
      }}>
        <DialogHeader>
          <DialogTitle>Delete saved view?</DialogTitle>
          <DialogDescription>
            {view.shared ? `“${view.name}” is shared, so it disappears for everyone, and weekly emails of it stop.` : `“${view.name}” is removed from your saved views, and its weekly email stops.`} Records are not affected.
          </DialogDescription>
        </DialogHeader>
        {error && <p role="alert" className="text-sm text-error-foreground">{error}</p>}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button type="button" variant="destructive" disabled={busy} onClick={() => void remove()}>{busy ? "Deleting…" : `Delete “${view.name}”`}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ---- Weekly email ---------------------------------------------------------------------------------------------------------

/** The organization's notification send time: its digest time, at the latest 23:00 (notify/rules.send_time). Only used
 *  before a subscription exists; a subscription carries its own `send_time`. */
const sendTimeOf = (p?: Preferences) => (p ? (p.digest_time > "23:00" ? "23:00" : p.digest_time) : "");
/** "Sent Wednesdays at 08:00 Pacific/Auckland" */
export const scheduleText = (weekday: number, time: string, timezone: string) => `Sent ${WEEKDAYS[weekday]}s at ${time} ${timezone}`;

function WeekdaySelect({ id, value, onChange, label = "Day", errors }: {
  id: string; value: number; onChange: (d: number) => void; label?: string; errors?: Record<string, string[]>;
}) {
  return (
    <Field id={id} label={label} errors={errors}>
      <Select id={id} className="w-48" value={String(value)} onChange={(e) => onChange(Number(e.target.value))}>
        {WEEKDAYS.map((d, i) => <option key={d} value={i}>{d}</option>)}
      </Select>
    </Field>
  );
}

type TestOutcome = { ok: boolean; text: string };

export function SubscriptionDialog({ open, onOpenChange, view, onChanged }: {
  open: boolean; onOpenChange: (o: boolean) => void; view: SavedFilter; onChanged: (text: string) => void;
}) {
  const returnFocus = useReturnFocus(open);
  const subId = view.subscription?.id ?? "";
  const path = `saved-view-subscriptions/${subId}`;
  // Read with its ETag when the dialog opens: a change saves with If-Match.
  const sub = useQuery({
    queryKey: [...apiKey(path), "etag"], queryFn: () => apiWithEtag<ViewSubscription>(path),
    enabled: open && !!subId, gcTime: 0, staleTime: 0, retry: false,
  });
  const prefs = useApiQuery<Preferences>("auth/me/preferences", { enabled: open, staleTime: 0 });
  const [weekday, setWeekday] = useState(0);
  const [active, setActive] = useState(true);
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [stale, setStale] = useState(false);
  const [busy, setBusy] = useState<"" | "save" | "test" | "remove">("");
  const [tested, setTested] = useState<TestOutcome | null>(null);
  const current = sub.data?.data;
  useEffect(() => {
    if (!open) return;
    setErrors({}); setStale(false); setTested(null);
    if (!subId) { setWeekday(0); setActive(true); }
  }, [open, subId]);
  // Each time the dialog opens, the form starts from the saved subscription (Cancel discards a changed day): the effect
  // runs on `open` too, because a refetch that returns the same values keeps the same `current` object.
  useEffect(() => { if (open && current) { setWeekday(current.weekday); setActive(current.active); } }, [open, current]);

  const time = current?.send_time ?? sendTimeOf(prefs.data);
  const zone = current?.timezone ?? prefs.data?.timezone ?? "";
  const emailsOff = prefs.data?.subscriptions_enabled === false;

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setBusy("save");
    setErrors({});
    try {
      if (!subId) {
        await api<ViewSubscription>("saved-view-subscriptions", { method: "POST", body: { saved_view: view.id, weekday, active } });
        onOpenChange(false);
        onChanged(`Subscribed to “${view.name}”: ${scheduleText(weekday, time, zone)}${active ? "" : " once you turn it on"}.`);
      } else {
        const etag = sub.data?.etag;
        await api<ViewSubscription>(path, { method: "PATCH", body: { weekday, active }, headers: etag ? { "If-Match": etag } : undefined });
        onOpenChange(false);
        onChanged(active ? `Weekly email of “${view.name}” saved: ${scheduleText(weekday, time, zone)}.` : `Weekly email of “${view.name}” turned off. Nothing is sent until you turn it back on.`);
      }
    } catch (err) {
      if (err instanceof Problem && err.status === 412) setStale(true);
      else if (err instanceof Problem) {
        const out: Record<string, string[]> = {};
        for (const [k, v] of Object.entries(err.errors)) {
          const key = k === "active" ? "sub-active" : "sub-weekday";
          out[key] = [...(out[key] ?? []), ...v];
        }
        setErrors(Object.keys(out).length ? out : { "sub-weekday": [err.detail] });
      } else setErrors({ "sub-weekday": ["Couldn't save the weekly email. Try again."] });
    } finally {
      setBusy("");
    }
  }
  async function test() {
    setBusy("test");
    setTested(null);
    try {
      const done = await api<SubscriptionTestResult>(`${path}/test-send`, { method: "POST" });
      setTested(done.ok ? { ok: true, text: "Test email sent to you. It is in the notification log as a test." } : { ok: false, text: `The test email wasn't sent: ${done.detail}` });
    } catch (err) {
      setTested({ ok: false, text: err instanceof Problem ? err.detail : "Couldn't send the test email. Try again." });
    } finally {
      setBusy("");
    }
  }
  async function unsubscribe() {
    setBusy("remove");
    try {
      // With the ETag of the read the dialog opened with; a 412 offers the latest version, as a save does.
      await api(path, { method: "DELETE", headers: ifMatch(sub.data?.etag) });
      onOpenChange(false);
      onChanged(`Unsubscribed from “${view.name}”. No more weekly emails of it.`);
    } catch (err) {
      if (isStaleWrite(err)) setStale(true);
      else setErrors({ "sub-weekday": [err instanceof Problem ? err.detail : "Couldn't unsubscribe. Try again."] });
    } finally {
      setBusy("");
    }
  }
  const loading = !!subId && sub.isPending;
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>Weekly email: {view.name}</DialogTitle>
          <DialogDescription>Once a week, an email with the records this view shows then (up to 50) and a link that opens it here.</DialogDescription>
        </DialogHeader>
        {loading && <Loading what="your weekly email" />}
        {sub.isError && <ErrorBanner message={`${sub.error instanceof Problem ? sub.error.detail : "Couldn't load your weekly email."} Nothing was changed.`} onRetry={() => void sub.refetch()} />}
        {!loading && !sub.isError && (
          <form onSubmit={save} noValidate>
            {stale && <StaleNote onLoad={() => { setStale(false); void sub.refetch(); }} />}
            <ErrorSummary errors={errors} />
            {emailsOff && (
              <p className="mb-3 rounded-md border border-l-4 border-state-pending-border bg-state-pending px-3 py-2 text-sm text-state-pending-foreground">
                <span aria-hidden="true">◐ </span>Saved view emails are turned off in <Link to="/profile" className="text-state-pending-foreground underline">your profile</Link>, so nothing is sent until you turn them on.
              </p>
            )}
            <WeekdaySelect id="sub-weekday" value={weekday} onChange={setWeekday} errors={errors} />
            <div className="mb-3">
              <label className="flex items-center gap-2 text-sm">
                <input id="sub-active" type="checkbox" checked={active} onChange={(e) => setActive(e.target.checked)} className="h-4 w-4 accent-primary"
                  aria-invalid={errors["sub-active"] ? true : undefined} aria-describedby={errors["sub-active"] ? "sub-active-error" : undefined} />
                Send the weekly email
              </label>
              {errors["sub-active"] && <p id="sub-active-error" className="mt-1 text-sm text-error-foreground">{errors["sub-active"].join(" ")}</p>}
            </div>
            <dl className="mb-3 grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 text-sm">
              <dt className="text-muted-foreground">When</dt>
              <dd data-testid="sub-schedule">{time && zone ? scheduleText(weekday, time, zone) : "On the day you choose, at the organization's notification time"}{active ? "" : " (turned off)"}</dd>
              {current && <>
                <dt className="text-muted-foreground">Last sent</dt>
                <dd data-testid="sub-last-sent">{current.last_sent ? <time dateTime={current.last_sent}>{current.last_sent}</time> : "Not sent yet"}</dd>
              </>}
            </dl>
            {current && (
              <div className="mb-4 flex flex-wrap items-center gap-2">
                <Button type="button" size="sm" variant="outline" disabled={busy !== ""} onClick={() => void test()}>{busy === "test" ? "Sending…" : "Send me a test now"}</Button>
                <Button type="button" size="sm" variant="outline" disabled={busy !== ""} onClick={() => void unsubscribe()}>{busy === "remove" ? "Unsubscribing…" : "Unsubscribe"}</Button>
              </div>
            )}
            <div role="status" className="text-sm">
              {tested?.ok && <p className="mb-3 rounded-md border border-success-border bg-success-bg px-3 py-2 text-success-foreground"><span aria-hidden="true">✓ </span>{tested.text}</p>}
            </div>
            {tested && !tested.ok && <p role="alert" className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2 text-sm">{tested.text}</p>}
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
              <Button type="submit" disabled={busy !== ""}>{busy === "save" ? "Saving…" : subId ? "Save" : "Subscribe"}</Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}

// ---- Subscribers (Admins) -------------------------------------------------------------------------------------------------

type Member = UserRef & { role: string; active: boolean };

const SubStatus = ({ s }: { s: ViewSubscription }) => (s.active
  ? <span className={chip({ state: "confirmed" })}><span aria-hidden="true">✓</span>On</span>
  : <span className={chip({ state: "neutral" })}><span aria-hidden="true">○</span>Turned off</span>);

export function SubscribersDialog({ open, onOpenChange, view, onChanged }: {
  open: boolean; onOpenChange: (o: boolean) => void; view: SavedFilter; onChanged: () => void;
}) {
  const returnFocus = useReturnFocus(open);
  const listPath = `saved-view-subscriptions?saved_view=${view.id}`;
  const subs = useApiQuery<ViewSubscription[]>(listPath, { enabled: open, staleTime: 0 });
  const users = useApiQuery<Member[]>("users", { enabled: open, staleTime: 30_000 });
  const invalidate = useInvalidate();
  const [member, setMember] = useState("");
  const [weekday, setWeekday] = useState(0);
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [note, setNote] = useState("");
  const [removeError, setRemoveError] = useState("");
  const [busy, setBusy] = useState("");
  useEffect(() => { if (open) { setMember(""); setWeekday(0); setErrors({}); setNote(""); setRemoveError(""); } }, [open]);
  const taken = new Set((subs.data ?? []).map((s) => s.user.id));
  const offered = (users.data ?? []).filter((u) => u.active && !taken.has(u.id));
  async function add(e: React.FormEvent) {
    e.preventDefault();
    if (!member) return setErrors({ "subscriber-member": ["Choose a member."] });
    setBusy("add");
    setErrors({});
    try {
      const made = await api<ViewSubscription>("saved-view-subscriptions", { method: "POST", body: { saved_view: view.id, user: Number(member), weekday } });
      setNote(`${made.user.name} subscribed: ${scheduleText(made.weekday, made.send_time, made.timezone)}.`);
      setMember("");
      await invalidate(listPath);
      onChanged();
    } catch (err) {
      if (err instanceof Problem) {
        const out: Record<string, string[]> = {};
        for (const [k, v] of Object.entries(err.errors)) {
          const key = k === "weekday" ? "subscriber-day" : "subscriber-member";
          out[key] = [...(out[key] ?? []), ...v];
        }
        setErrors(Object.keys(out).length ? out : { "subscriber-member": [err.detail] });
      } else setErrors({ "subscriber-member": ["Couldn't subscribe the member. Try again."] });
    } finally {
      setBusy("");
    }
  }
  async function remove(s: ViewSubscription) {
    setBusy(s.id);
    setRemoveError("");
    try {
      // One click on the row, no confirmation and nothing read sent back: any revision.
      await api(`saved-view-subscriptions/${s.id}`, { method: "DELETE", headers: ANY_REVISION });
      setNote(`${s.user.name} unsubscribed.`);
      await invalidate(listPath);
      onChanged();
      setTimeout(() => document.getElementById("subscriber-member")?.focus());
    } catch (err) {
      setNote("");
      setRemoveError(`${s.user.name} wasn't unsubscribed: ${err instanceof Problem ? err.detail : "something went wrong. Try again."}`);
    } finally {
      setBusy("");
    }
  }
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>Subscribers: {view.name}</DialogTitle>
          <DialogDescription>Who gets this shared view by email each week. Each member gets only the records their own role lets them see.</DialogDescription>
        </DialogHeader>
        <p role="status" className="text-sm">{note}</p>
        {removeError && <p role="alert" className="text-sm text-error-foreground"><span aria-hidden="true">! </span>{removeError}</p>}
        {subs.isError && <ErrorBanner message={`${subs.error instanceof Problem ? subs.error.detail : "Couldn't load the subscribers."} Nothing was changed.`} onRetry={() => void subs.refetch()} />}
        {subs.isPending && <Loading what="subscribers" />}
        {subs.data && subs.data.length === 0 && <p className="text-sm text-muted-foreground">Nobody gets this view by email yet.</p>}
        {subs.data && subs.data.length > 0 && (
          <TableRegion label="Subscribers table">
            <Table>
              <TableCaption className="sr-only">Subscribers to {view.name}</TableCaption>
              <TableHeader><TableRow>
                <TableHead scope="col">Member</TableHead><TableHead scope="col">Day</TableHead><TableHead scope="col">Status</TableHead>
                <TableHead scope="col">Last sent</TableHead><TableHead scope="col"><span className="sr-only">Actions</span></TableHead>
              </TableRow></TableHeader>
              <TableBody>
                {subs.data.map((s) => (
                  <TableRow key={s.id}>
                    <TableCell>{s.user.name}</TableCell>
                    <TableCell>{s.weekday_name}</TableCell>
                    <TableCell><SubStatus s={s} /></TableCell>
                    <TableCell className="tabular">{s.last_sent ?? "Not sent yet"}</TableCell>
                    <TableCell className="text-right">
                      <Button size="sm" variant="outline" disabled={busy !== ""} onClick={() => void remove(s)}>Remove {s.user.name}</Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableRegion>
        )}
        <form onSubmit={add} noValidate className="mt-2">
          <h3 className="mb-2 text-sm font-semibold">Subscribe a member</h3>
          <ErrorSummary errors={errors} />
          <div className="flex flex-wrap items-end gap-x-3">
            <Field id="subscriber-member" label="Member" errors={errors}>
              <Select id="subscriber-member" className="w-64" value={member} onChange={(e) => setMember(e.target.value)} disabled={!users.data}>
                <option value="">{users.data ? (offered.length ? "Choose a member…" : "Everyone is subscribed") : "Loading members…"}</option>
                {offered.map((u) => <option key={u.id} value={u.id}>{u.name} ({u.email})</option>)}
              </Select>
            </Field>
            <Field id="subscriber-day" label="Day" errors={errors}>
              <Select id="subscriber-day" className="w-40" value={String(weekday)} onChange={(e) => setWeekday(Number(e.target.value))}>
                {WEEKDAYS.map((d, i) => <option key={d} value={i}>{d}</option>)}
              </Select>
            </Field>
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>Close</Button>
            <Button type="submit" disabled={busy !== ""}>{busy === "add" ? "Subscribing…" : "Subscribe member"}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

// ---- The profile's list -----------------------------------------------------------------------------------------------------

/** The member's own weekly emails (profile): where each view opens, its day and state, and Remove (the only way to end the
 *  subscription of a view that was deleted). */
export function MySubscriptions() {
  const me = useMe();
  const path = `saved-view-subscriptions?user=${me.user.id}`;
  const subs = useApiQuery<ViewSubscription[]>(path, { staleTime: 0 });
  const invalidate = useInvalidate();
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  async function remove(s: ViewSubscription) {
    setBusy(s.id);
    setError("");
    try {
      // One click on the row, no confirmation and nothing read sent back: any revision.
      await api(`saved-view-subscriptions/${s.id}`, { method: "DELETE", headers: ANY_REVISION });
      setNote(`Unsubscribed from ${s.saved_view?.name ? `“${s.saved_view.name}”` : "the deleted view"}.`);
      await invalidate(path);
      setTimeout(() => document.getElementById("my-subscriptions-heading")?.focus());
    } catch (err) {
      setError(err instanceof Problem ? err.detail : "Couldn't unsubscribe. Try again.");
    } finally {
      setBusy("");
    }
  }
  const rows = subs.data;
  const nameOf = (s: ViewSubscription) => s.saved_view?.name ?? (s.saved_view ? "A private view" : "");
  return (
    <section aria-labelledby="my-subscriptions-heading" className="mt-4">
      <h3 id="my-subscriptions-heading" tabIndex={-1} className="mb-2 font-semibold outline-none">Your weekly emails</h3>
      <p role="status" className="text-sm">{note}</p>
      {error && <ErrorBanner message={error} />}
      {subs.isError && <ErrorBanner message={`${subs.error instanceof Problem ? subs.error.detail : "Couldn't load your weekly emails."} Nothing was changed.`} onRetry={() => void subs.refetch()} />}
      {subs.isPending && <Loading what="your weekly emails" />}
      {rows && rows.length === 0 && <p className="text-sm text-muted-foreground">You don't get any saved view by email. Open a saved view on Applications, Components or AI agents and choose Email me weekly.</p>}
      {rows && rows.length > 0 && (
        <TableRegion label="Your weekly emails table">
          <Table>
            <TableCaption className="sr-only">Your weekly saved view emails</TableCaption>
            <TableHeader><TableRow>
              <TableHead scope="col">View</TableHead><TableHead scope="col">Day</TableHead><TableHead scope="col">Status</TableHead>
              <TableHead scope="col">Last sent</TableHead><TableHead scope="col"><span className="sr-only">Actions</span></TableHead>
            </TableRow></TableHeader>
            <TableBody>
              {rows.map((s) => (
                <TableRow key={s.id}>
                  <TableCell>{s.saved_view ? <Link to={viewPath(s.saved_view.kind, s.saved_view.id)}>{nameOf(s)}</Link> : <GapChip label="View deleted" />}</TableCell>
                  <TableCell>{s.weekday_name}</TableCell>
                  <TableCell><SubStatus s={s} /></TableCell>
                  <TableCell className="tabular">{s.last_sent ?? "Not sent yet"}</TableCell>
                  <TableCell className="text-right">
                    <Button size="sm" variant="outline" disabled={busy !== ""} onClick={() => void remove(s)}>Unsubscribe from {nameOf(s) || "the deleted view"}</Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableRegion>
      )}
      {rows && rows.length > 0 && <p className="mt-2 text-sm text-muted-foreground">Sent at {rows[0].send_time} {rows[0].timezone} on each day. Change a day from the view's Weekly email button.</p>}
    </section>
  );
}
