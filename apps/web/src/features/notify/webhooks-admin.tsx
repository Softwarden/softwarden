/** Administration › Webhooks: signed HTTPS endpoints and the events each receives.
 *  Add and edit (name, description, on or off, the address and the signing secret, both write-only: the page shows the
 *  address's host and since when each is set, never a value), events picked by family with family wildcards, a ping
 *  with its answer inline, secret rotation with a grace period, and delete. An endpoint Softwarden turned off (410 Gone,
 *  or too many failed event deliveries) says so with the reason. Admin only, capability `webhooks`.
 *
 *  This is the SPA's generated-client feature: every request and response here is typed from
 *  src/generated/openapi.d.ts through src/lib/openapi.ts, so the page can't drift from the schema. Loaded on first use. */
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { PRECONDITION_FAILED, Problem, when } from "@/api";
import {
  AdminOnly, EmptyState, ErrorBanner, ErrorSummary, Field, Loading, PageHead, Select, TableRegion, Textarea, useReturnFocus,
} from "@/components/app";
import { MoreMenu } from "@/components/more-menu";
import { chip, SuccessNote } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { typedApi, typedGetWithEtag, useTypedQuery, type BodyOf, type Schemas } from "@/lib/openapi";
import { apiKey, problemText, useInvalidate } from "@/lib/query";
import { useRefreshMe } from "@/lib/session";
import {
  answerText, EndpointStateChip, eventsText, generateSecret, LastDeliveryCell, LOG_PAGE, useHasWebhooks,
  WebhooksUnavailable, WHAT_ENDPOINTS, type EventCatalog, type Subscription, type Webhook,
} from "@/features/notify/webhooks-ui";

const LIST = "webhooks";

export function WebhooksPage() {
  return <AdminOnly title="Webhooks" what={WHAT_ENDPOINTS}><Gate /></AdminOnly>;
}

function Gate() {
  if (!useHasWebhooks()) {
    return <><PageHead title="Webhooks" /><WebhooksUnavailable /></>;
  }
  return <Endpoints />;
}

type PingOutcome = { name: string; result?: Schemas["PingResult"]; error?: string };

function Endpoints() {
  const q = useTypedQuery("webhooks");
  const invalidate = useInvalidate();
  const navigate = useNavigate();
  const refreshMe = useRefreshMe();
  const [editing, setEditing] = useState<Webhook | "new" | null>(null);
  const [rotating, setRotating] = useState<Webhook | null>(null);
  const [deleting, setDeleting] = useState<Webhook | null>(null);
  const [notice, setNotice] = useState("");
  const [ping, setPing] = useState<PingOutcome | null>(null);
  const [busy, setBusy] = useState("");
  const rows = q.data;

  async function sendPing(w: Webhook) {
    setBusy(w.id);
    setPing(null);
    try {
      const result = await typedApi("post", "webhooks/{id}/ping", { path: { id: w.id } });
      setPing({ name: w.name, result });
      void invalidate(LIST, "webhook-deliveries");
    } catch (e) {
      setPing({ name: w.name, error: problemText(e, "Couldn't send the ping.") });
    } finally {
      setBusy("");
    }
  }
  const done = (text: string) => {
    setEditing(null); setRotating(null); setDeleting(null); setNotice(text);
    void invalidate(LIST, "notification-settings");
    // Turning an endpoint back on (or deleting it) clears the Admin banner.
    refreshMe();
  };
  const add = <Button onClick={() => setEditing("new")}>Add webhook</Button>;
  return (
    <>
      <PageHead title="Webhooks" desc="Softwarden posts signed JSON to these HTTPS endpoints when records, relationships, renewals, discovery candidates or reminders change.">
        <Button asChild variant="outline"><Link to={LOG_PAGE}>Delivery log</Link></Button>
        {rows && rows.length > 0 && add}
      </PageHead>
      <p className="mb-4 max-w-prose text-sm text-muted-foreground">
        Every post carries an X-Softwarden-Signature header (HMAC SHA-256 under the endpoint's signing secret); <Link className="underline" to="/admin/api/guide#verifying-a-webhook">how to verify it</Link>.
        By default, failed posts are retried for about nine hours, and after five event deliveries in a row fail, or one is answered 410 Gone, Softwarden turns the endpoint off (the server's settings can change both numbers).
        Notification rules can post their reminder notices to these endpoints too (<Link className="underline" to="/admin/notification-rules">Notification rules</Link>).
      </p>
      <SuccessNote text={notice} />
      {q.isError && <ErrorBanner message={problemText(q.error, "Couldn't load the webhooks.")} onRetry={() => void q.refetch()} />}
      {!rows && !q.isError && <Loading what="webhooks" rows={3} />}
      {rows && rows.length === 0 && (
        <EmptyState title="No webhooks yet" action={add}>
          Add an HTTPS address and a signing secret, then choose the events it receives. Ping it to check that it answers.
        </EmptyState>
      )}
      {rows && rows.length > 0 && (
        <TableRegion label="Webhooks table">
          <Table>
            <TableCaption className="sr-only">Webhook endpoints, by name</TableCaption>
            <TableHeader><TableRow>
              <TableHead scope="col">Name</TableHead><TableHead scope="col">Status</TableHead><TableHead scope="col">Address</TableHead>
              <TableHead scope="col">Signing secret</TableHead><TableHead scope="col">Events</TableHead><TableHead scope="col">Last delivery</TableHead>
              <TableHead scope="col"><span className="sr-only">Actions</span></TableHead>
            </TableRow></TableHeader>
            <TableBody>
              {rows.map((w) => (
                <TableRow key={w.id} className="align-top">
                  <TableHead scope="row" className="min-w-40 max-w-xs whitespace-normal font-semibold text-foreground [overflow-wrap:anywhere]">
                    {w.name}
                    {w.description && <span className="block text-sm font-normal text-muted-foreground">{w.description}</span>}
                  </TableHead>
                  <TableCell className="max-w-64 whitespace-normal">
                    <EndpointStateChip w={w} />
                    {w.disabled_at && (
                      <span className="mt-1 block text-sm [overflow-wrap:anywhere]">
                        On <span className="tabular">{when(w.disabled_at)}</span>{w.disabled_reason ? `: ${w.disabled_reason}` : "."}
                      </span>
                    )}
                    {w.consecutive_failures > 0 && (
                      <span className="mt-1 block text-sm text-error-foreground">
                        <span aria-hidden="true">! </span>{w.consecutive_failures} failed event {w.consecutive_failures === 1 ? "delivery" : "deliveries"} in a row
                      </span>
                    )}
                  </TableCell>
                  <TableCell className="whitespace-normal">
                    <span className="[overflow-wrap:anywhere]">{w.url_host}</span>
                    <span className="block text-sm text-muted-foreground">Set on {w.url_set_on ?? "an earlier date"}; the full address is never shown</span>
                  </TableCell>
                  <TableCell className="whitespace-normal">
                    <SecretCell w={w} />
                  </TableCell>
                  <TableCell className="max-w-56 whitespace-normal">
                    {w.events.length
                      ? <span className="font-mono text-sm [overflow-wrap:anywhere]">{eventsText(w.events)}</span>
                      : <span className={chip({ state: "gap" })}><span aria-hidden="true">⚠</span>No events</span>}
                  </TableCell>
                  <TableCell className="whitespace-normal"><LastDeliveryCell w={w} /></TableCell>
                  <TableCell className="text-right">
                    <span className="inline-flex flex-wrap justify-end gap-2">
                      <Button size="sm" variant="outline" disabled={busy === w.id} onClick={() => void sendPing(w)}>
                        {busy === w.id ? "Pinging…" : "Ping"}<span className="sr-only"> {w.name}</span>
                      </Button>
                      <Button size="sm" variant="outline" onClick={() => setEditing(w)}>Edit<span className="sr-only"> {w.name}</span></Button>
                      <MoreMenu size="sm" label="More" about={w.name} actions={[
                        { label: "Rotate secret…", onSelect: () => setRotating(w) },
                        { label: "View deliveries", onSelect: () => navigate(`${LOG_PAGE}?webhook=${w.id}`) },
                        { label: "Delete…", onSelect: () => setDeleting(w), danger: true },
                      ]} />
                    </span>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableRegion>
      )}
      <div className="mt-3" role="status" data-testid="ping-result">{ping && <PingResultNote ping={ping} />}</div>
      <EndpointDialog item={editing} onClose={() => setEditing(null)} onDone={done} />
      <RotateDialog item={rotating} onClose={() => setRotating(null)} onDone={done} />
      <DeleteDialog item={deleting} onClose={() => setDeleting(null)} onDone={done} />
    </>
  );
}

function SecretCell({ w }: { w: Webhook }) {
  const rotating = w.previous_secret_expires_at && new Date(w.previous_secret_expires_at) > new Date();
  return (
    <span className="inline-flex flex-col items-start gap-1">
      {w.signing_secret_set_on
        ? <span className={chip({ state: "confirmed" })}><span aria-hidden="true">✓</span>Set on {w.signing_secret_set_on}</span>
        : <span className={chip({ state: "gap" })}><span aria-hidden="true">⚠</span>No signing secret</span>}
      {rotating && (
        <span className={chip({ state: "pending" })}><span aria-hidden="true">◐</span>Previous secret signs until {when(w.previous_secret_expires_at)}</span>
      )}
    </span>
  );
}

/** A ping's answer in words, glyph first, linked to its row in the delivery log. */
function PingResultNote({ ping }: { ping: PingOutcome }) {
  if (ping.error) {
    return <p className="rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2"><span aria-hidden="true">! </span>Ping to {ping.name} not sent: {ping.error}</p>;
  }
  const r = ping.result!;
  const attempt = r.delivery.attempt_log[r.delivery.attempt_log.length - 1];
  const timing = attempt?.duration_ms !== null && attempt?.duration_ms !== undefined ? ` in ${attempt.duration_ms} ms` : "";
  return (
    <div className={`rounded-md border px-3 py-2 ${r.ok ? "border-success-border bg-success-bg text-success-foreground" : "border-l-4 border-error-border bg-error-bg"}`}>
      <p className="font-semibold">
        <span aria-hidden="true">{r.ok ? "✓ " : "! "}</span>
        {r.ok ? `Ping to ${ping.name}: the endpoint answered ${answerText(attempt?.status_code)}${timing}.` : `Ping to ${ping.name} failed: ${r.detail}`}
      </p>
      {!r.ok && attempt && <p className="text-sm">{answerText(attempt.status_code)}{timing}.</p>}
      <Link to={`${LOG_PAGE}/${r.delivery.id}`} className="text-sm">View the ping in the delivery log<span className="sr-only"> for {ping.name}</span></Link>
    </div>
  );
}

// ---- add and edit -------------------------------------------------------------------------------------------------

function EndpointDialog({ item, onClose, onDone }: { item: Webhook | "new" | null; onClose: () => void; onDone: (text: string) => void }) {
  const open = item !== null;
  const returnFocus = useReturnFocus(open);
  // Kept while the dialog animates closed, so its title doesn't change on the way out.
  const [last, setLast] = useState(item);
  if (item && item !== last) setLast(item);
  const shown = item ?? last;
  const existing = shown && shown !== "new" ? shown : null;
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-2xl" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>{existing ? `Edit webhook ${existing.name}` : "Add webhook"}</DialogTitle>
          <DialogDescription>
            {existing ? "Leave the address and the signing secret empty to keep the stored ones; neither is ever shown." : "The address and the signing secret are stored encrypted and never shown again; only the address's host is."}
          </DialogDescription>
        </DialogHeader>
        {open && (existing ? <EditLoader key={existing.id} id={existing.id} onCancel={onClose} onDone={onDone} /> : <EndpointForm existing={null} etag={null} onCancel={onClose} onDone={onDone} onReload={() => undefined} />)}
      </DialogContent>
    </Dialog>
  );
}

/** Edits start from the endpoint as stored now, with its ETag, so a change someone else made meanwhile is a 412. */
function EditLoader({ id, onCancel, onDone }: { id: string; onCancel: () => void; onDone: (text: string) => void }) {
  const q = useEndpointWithEtag(id);
  if (q.error) return <ErrorBanner message={q.error} onRetry={q.reload} />;
  if (!q.data || q.fetching) return <Loading what="the webhook" />;
  return <EndpointForm key={q.data.etag ?? "none"} existing={q.data.data} etag={q.data.etag} onCancel={onCancel} onDone={onDone} onReload={q.reload} />;
}

/** The endpoint as stored now with its ETag, fetched afresh each time a dialog opens (never from the cache). */
function useEndpointWithEtag(id: string) {
  const q = useQuery({ queryKey: [...apiKey(`webhooks/${id}`), "etag"], queryFn: () => typedGetWithEtag("webhooks/{id}", { path: { id } }), gcTime: 0, staleTime: 0 });
  const invalidate = useInvalidate();
  // "Load the latest version" after a 412 also reloads the list behind the dialog, which shows the same change.
  const reload = () => { void q.refetch(); void invalidate(LIST); };
  return { data: q.data, error: q.isError && !q.isFetching ? problemText(q.error, "Couldn't load the webhook.") : "", reload, fetching: q.isFetching };
}

type Form = { name: string; description: string; enabled: boolean; url: string; signing_secret: string; events: Subscription[] };

function EndpointForm({ existing, etag, onCancel, onDone, onReload }: {
  existing: Webhook | null; etag: string | null; onCancel: () => void; onDone: (text: string) => void; onReload: () => void;
}) {
  const catalog = useTypedQuery("webhooks/event-types");
  const [form, setForm] = useState<Form>({
    name: existing?.name ?? "", description: existing?.description ?? "", enabled: existing?.enabled ?? true,
    url: "", signing_secret: "", events: existing ? [...existing.events] : [],
  });
  const [revealed, setRevealed] = useState(false);
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [stale, setStale] = useState(false);
  const [saving, setSaving] = useState(false);
  const set = <K extends keyof Form>(k: K, v: Form[K]) => setForm((f) => ({ ...f, [k]: v }));

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const missing: Record<string, string[]> = {};
    if (!form.name.trim()) missing.name = ["Enter a name."];
    if (!existing && !form.url.trim()) missing.url = ["Enter the endpoint's https:// address."];
    if (!existing && !form.signing_secret) missing.signing_secret = ["Enter a signing secret of 16 characters or more, or generate one."];
    if (Object.keys(missing).length) return setErrors(missing);
    setSaving(true);
    try {
      if (existing) {
        const body: BodyOf<"webhooks/{id}", "patch"> = {};
        if (form.name.trim() !== existing.name) body.name = form.name.trim();
        if (form.description !== existing.description) body.description = form.description;
        if (form.enabled !== existing.enabled) body.enabled = form.enabled;
        if (form.url.trim()) body.url = form.url.trim();
        if (form.signing_secret) body.signing_secret = form.signing_secret;
        // Compared as sets: the API keeps the catalog's order, the picker the order of ticking.
        if ([...form.events].sort().join() !== [...existing.events].sort().join()) body.events = form.events;
        if (!Object.keys(body).length) return onDone("Nothing changed.");
        await typedApi("patch", "webhooks/{id}", { path: { id: existing.id }, body, headers: etag ? { "If-Match": etag } : {} });
        onDone(`Webhook ${form.name.trim()} saved.`);
      } else {
        await typedApi("post", "webhooks", {
          body: { name: form.name.trim(), description: form.description, enabled: form.enabled, url: form.url.trim(), signing_secret: form.signing_secret, events: form.events },
        });
        onDone(`Webhook ${form.name.trim()} added. Ping it to check that it answers.`);
      }
    } catch (err) {
      if (err instanceof Problem && err.status === 412 && err.type === PRECONDITION_FAILED) setStale(true);
      else setErrors(err instanceof Problem && Object.keys(err.errors).length ? err.errors : { name: [problemText(err, "Couldn't save the webhook.")] });
    } finally {
      setSaving(false);
    }
  }
  const p = "wh-";
  const shown = Object.fromEntries(Object.entries(errors).map(([k, v]) => [`${p}${k}`, v]));
  return (
    <form onSubmit={submit} noValidate>
      {stale && (
        <div className="mb-3 rounded-md border border-l-4 border-state-pending-border bg-state-pending px-4 py-3 text-state-pending-foreground" role="alert">
          <p className="font-semibold">Someone changed this webhook since you opened it. Nothing was saved.</p>
          <div className="mt-2"><Button type="button" size="sm" variant="outline" onClick={onReload}>Load the latest version</Button></div>
        </div>
      )}
      <ErrorSummary errors={shown} />
      {existing?.disabled_at && (
        <p className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2 text-sm" role="note">
          <span aria-hidden="true">! </span>Softwarden turned this endpoint off on {when(existing.disabled_at)}{existing.disabled_reason ? `: ${existing.disabled_reason}` : "."} Turning it on again clears the failure count. Events from while it was off aren't sent; resend them from the delivery log.
        </p>
      )}
      <Field id={`${p}name`} label="Name (required)" errors={shown} help="The delivery log and notification rules show this name.">
        <Input id={`${p}name`} value={form.name} onChange={(e) => set("name", e.target.value)} maxLength={100} />
      </Field>
      <Field id={`${p}description`} label="Description" errors={shown} help="What receives it, such as the ticketing system's intake.">
        <Textarea id={`${p}description`} rows={2} value={form.description} onChange={(e) => set("description", e.target.value)} maxLength={500} />
      </Field>
      <label className="mb-3 flex items-center gap-2 text-sm">
        <input type="checkbox" checked={form.enabled} onChange={(e) => set("enabled", e.target.checked)} className="h-4 w-4 accent-primary" />
        On (Softwarden posts events to it)
      </label>
      {existing && (
        <p className="mb-1.5 text-sm"><span className={chip({ state: "confirmed" })}><span aria-hidden="true">✓</span>Address set on {existing.url_set_on ?? "an earlier date"} ({existing.url_host})</span></p>
      )}
      <Field id={`${p}url`} label={existing ? "Replace address" : "Address (required)"} errors={shown}
        help="An https:// address on a public host. It can carry a token, so it is kept secret: only its host is shown.">
        <Input id={`${p}url`} type="password" value={form.url} onChange={(e) => set("url", e.target.value)} autoComplete="off" spellCheck={false} />
      </Field>
      {existing && (
        <p className="mb-1.5 text-sm">
          {existing.signing_secret_set_on
            ? <span className={chip({ state: "confirmed" })}><span aria-hidden="true">✓</span>Signing secret set on {existing.signing_secret_set_on}</span>
            : <span className={chip({ state: "gap" })}><span aria-hidden="true">⚠</span>No signing secret</span>}
        </p>
      )}
      <Field id={`${p}signing_secret`} label={existing ? "Replace signing secret" : "Signing secret (required)"} errors={shown}
        help={existing
          ? "Replacing it here stops the old secret at once. To give the receiver time to switch, use Rotate secret instead."
          : "16 characters or more. The receiver checks X-Softwarden-Signature with it. Copy it to the receiver before you save: it is never shown again."}>
        <Input id={`${p}signing_secret`} type={revealed ? "text" : "password"} value={form.signing_secret} onChange={(e) => set("signing_secret", e.target.value)}
          autoComplete="new-password" spellCheck={false} className="font-mono" />
      </Field>
      <SecretTools value={form.signing_secret} revealed={revealed} onReveal={setRevealed} onGenerate={(v) => { set("signing_secret", v); setRevealed(true); }} />
      <EventPicker catalog={catalog.data} loading={!catalog.data && !catalog.isError} error={catalog.isError ? problemText(catalog.error, "Couldn't load the event types.") : ""}
        onRetry={() => void catalog.refetch()} value={form.events} onChange={(v) => set("events", v)} errors={shown[`${p}events`]} />
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        <Button type="submit" disabled={saving}>{saving ? "Saving…" : existing ? "Save webhook" : "Add webhook"}</Button>
      </DialogFooter>
    </form>
  );
}

/** Generate a strong secret (shown, so it can be copied to the receiver), show or hide what was typed, and copy it. */
function SecretTools({ value, revealed, onReveal, onGenerate }: { value: string; revealed: boolean; onReveal: (v: boolean) => void; onGenerate: (v: string) => void }) {
  const [status, setStatus] = useState("");
  async function copy() {
    try { await navigator.clipboard.writeText(value); setStatus("Secret copied to the clipboard."); }
    catch { setStatus("Couldn't copy. Select the secret and copy it by hand."); }
  }
  return (
    <div className="-mt-1 mb-4">
      <div className="flex flex-wrap gap-2">
        <Button type="button" size="sm" variant="outline" onClick={() => { onGenerate(generateSecret()); setStatus("A new secret was generated and is shown. Copy it to the receiver before you save."); }}>Generate a secret</Button>
        <Button type="button" size="sm" variant="outline" aria-pressed={revealed} onClick={() => onReveal(!revealed)}>{revealed ? "Hide secret" : "Show secret"}</Button>
        {value && <Button type="button" size="sm" variant="outline" onClick={() => void copy()}>Copy secret</Button>}
      </div>
      <p className="mt-1 text-sm text-muted-foreground" role="status">{status}</p>
    </div>
  );
}

const FAMILY_LABEL: Record<string, string> = {
  record: "Records", relationship: "Relationships", renewal: "Renewals", discovery: "Discovery", reminder: "Reminders",
};

/** Events grouped by family: "Every … event" subscribes to the family's wildcard (types added later included), or
 *  pick types one by one. A type its family's wildcard covers shows as included and can't be unticked alone. */
function EventPicker({ catalog, loading, error, onRetry, value, onChange, errors }: {
  catalog: EventCatalog | undefined; loading: boolean; error: string; onRetry: () => void;
  value: Subscription[]; onChange: (v: Subscription[]) => void; errors?: string[];
}) {
  const families: { family: string; types: EventCatalog["types"] }[] = [];
  for (const t of catalog?.types ?? []) {
    const group = families.find((f) => f.family === t.family) ?? (families.push({ family: t.family, types: [] }), families[families.length - 1]);
    group.types.push(t);
  }
  const chosen = new Set<string>(value);
  const toggleWildcard = (family: string, on: boolean) => {
    const wildcard = `${family}.*` as Subscription;
    const rest = value.filter((v) => v !== wildcard && !v.startsWith(`${family}.`));
    onChange(on ? [...rest, wildcard] : rest);
  };
  const toggleType = (type: string, on: boolean) => onChange(on ? [...value, type as Subscription] : value.filter((v) => v !== type));
  return (
    <fieldset className="mb-4" aria-describedby="wh-events-help">
      <legend className="mb-1 font-display text-section font-semibold">Events</legend>
      <p id="wh-events-help" className="mb-2 text-sm text-muted-foreground">
        {value.length ? <>Chosen: <span className="font-mono">{eventsText(value)}</span>.</> : "No events chosen: the endpoint gets only pings, and the notices of notification rules that name it."}
      </p>
      {errors && <p className="mb-2 text-sm text-error-foreground" id="wh-events">{errors.join(" ")}</p>}
      {loading && <p className="text-sm text-muted-foreground" aria-live="polite">Loading the event types…</p>}
      {error && <ErrorBanner message={error} onRetry={onRetry} />}
      <div className="grid gap-3 sm:grid-cols-2">
        {families.map(({ family, types }) => {
          const wildcard = `${family}.*`;
          const all = chosen.has(wildcard);
          const label = FAMILY_LABEL[family] ?? family;
          return (
            <fieldset key={family} className="rounded-md border px-3 py-2">
              <legend className="px-1 text-sm font-semibold">{label}</legend>
              <label className="flex min-h-8 items-start gap-2 text-sm">
                <input type="checkbox" className="mt-0.5 h-4 w-4 accent-primary" checked={all} onChange={(e) => toggleWildcard(family, e.target.checked)} />
                <span>Every {label.toLowerCase()} event <span className="font-mono text-muted-foreground">({wildcard})</span></span>
              </label>
              <ul className="ml-6 mt-1 list-none p-0">
                {types.map((t) => {
                  const id = `wh-ev-${t.type.replace(/\W/g, "-")}`;
                  return (
                    <li key={t.type} className="mb-1">
                      <label className="flex min-h-6 items-start gap-2 text-sm">
                        <input type="checkbox" className="mt-0.5 h-4 w-4 accent-primary" checked={all || chosen.has(t.type)} disabled={all}
                          aria-describedby={`${id}-help`} onChange={(e) => toggleType(t.type, e.target.checked)} />
                        <span className="font-mono">{t.type}{all && <span className="sr-only"> (included in {wildcard})</span>}</span>
                      </label>
                      <p id={`${id}-help`} className="ml-6 text-xs text-muted-foreground">{t.description.replace(/`/g, "")}</p>
                    </li>
                  );
                })}
              </ul>
            </fieldset>
          );
        })}
      </div>
    </fieldset>
  );
}

// ---- rotate the secret --------------------------------------------------------------------------------------------

const GRACE = [
  { hours: 0, label: "None: stop the previous secret now" },
  { hours: 1, label: "1 hour" },
  { hours: 24, label: "24 hours (the default)" },
  { hours: 72, label: "3 days" },
  { hours: 168, label: "7 days (the most)" },
];

function RotateDialog({ item, onClose, onDone }: { item: Webhook | null; onClose: () => void; onDone: (text: string) => void }) {
  const open = item !== null;
  const returnFocus = useReturnFocus(open);
  const [last, setLast] = useState(item);
  if (item && item !== last) setLast(item);
  const shown = item ?? last;
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>Rotate the signing secret of {shown?.name}</DialogTitle>
          <DialogDescription>
            The new secret signs every post from now on. During the grace period each post also carries a second signature
            under the previous secret (a second v1= in X-Softwarden-Signature, after the new one), so the receiver can switch
            at any time in that window without refusing an event. After it, only the new secret signs.
          </DialogDescription>
        </DialogHeader>
        {open && item && <RotateForm key={item.id} item={item} onCancel={onClose} onDone={onDone} />}
      </DialogContent>
    </Dialog>
  );
}

function RotateForm({ item, onCancel, onDone }: { item: Webhook; onCancel: () => void; onDone: (text: string) => void }) {
  const loaded = useEndpointWithEtag(item.id);
  const [secret, setSecret] = useState("");
  const [revealed, setRevealed] = useState(false);
  const [grace, setGrace] = useState("24");
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [stale, setStale] = useState(false);
  const [saving, setSaving] = useState(false);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (secret.length < 16) return setErrors({ "rot-signing_secret": ["Enter a new secret of 16 characters or more, or generate one."] });
    setSaving(true);
    try {
      const etag = loaded.data?.etag;
      const done = await typedApi("post", "webhooks/{id}/rotate-secret", {
        path: { id: item.id }, body: { signing_secret: secret, grace_period_hours: Number(grace) }, headers: etag ? { "If-Match": etag } : {},
      });
      onDone(done.previous_secret_expires_at
        ? `Signing secret of ${item.name} rotated. The previous secret also signs until ${when(done.previous_secret_expires_at)}.`
        : `Signing secret of ${item.name} replaced. Only the new secret signs.`);
    } catch (err) {
      if (err instanceof Problem && err.status === 412 && err.type === PRECONDITION_FAILED) setStale(true);
      else setErrors(err instanceof Problem && Object.keys(err.errors).length
        ? Object.fromEntries(Object.entries(err.errors).map(([k, v]) => [`rot-${k}`, v]))
        : { "rot-signing_secret": [problemText(err, "Couldn't rotate the secret.")] });
    } finally {
      setSaving(false);
    }
  }
  if (loaded.error) return <ErrorBanner message={loaded.error} onRetry={loaded.reload} />;
  if (!loaded.data) return <Loading what="the webhook" />;
  const w = loaded.data.data;
  return (
    <form onSubmit={submit} noValidate>
      {stale && (
        <div className="mb-3 rounded-md border border-l-4 border-state-pending-border bg-state-pending px-4 py-3 text-state-pending-foreground" role="alert">
          <p className="font-semibold">Someone changed this webhook since you opened it. Nothing was rotated.</p>
          <div className="mt-2"><Button type="button" size="sm" variant="outline" onClick={() => { setStale(false); loaded.reload(); }}>Load the latest version</Button></div>
        </div>
      )}
      <ErrorSummary errors={errors} />
      <p className="mb-3 text-sm">
        {w.signing_secret_set_on
          ? <span className={chip({ state: "confirmed" })}><span aria-hidden="true">✓</span>Current secret set on {w.signing_secret_set_on}</span>
          : <span className={chip({ state: "gap" })}><span aria-hidden="true">⚠</span>No signing secret</span>}
      </p>
      <Field id="rot-signing_secret" label="New signing secret (required)" errors={errors}
        help="16 characters or more. Give it to the receiver; it is never shown again after you rotate.">
        <Input id="rot-signing_secret" type={revealed ? "text" : "password"} value={secret} onChange={(e) => setSecret(e.target.value)} autoComplete="new-password" spellCheck={false} className="font-mono" />
      </Field>
      <SecretTools value={secret} revealed={revealed} onReveal={setRevealed} onGenerate={(v) => { setSecret(v); setRevealed(true); }} />
      <Field id="rot-grace_period_hours" label="Grace period" errors={errors}
        help="How long the previous secret keeps signing beside the new one. Choose None only if the receiver already has the new secret.">
        <Select id="rot-grace_period_hours" value={grace} onChange={(e) => setGrace(e.target.value)}>
          {GRACE.map((g) => <option key={g.hours} value={g.hours}>{g.label}</option>)}
        </Select>
      </Field>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        <Button type="submit" disabled={saving}>{saving ? "Rotating…" : "Rotate secret"}</Button>
      </DialogFooter>
    </form>
  );
}

// ---- delete -------------------------------------------------------------------------------------------------------

function DeleteDialog({ item, onClose, onDone }: { item: Webhook | null; onClose: () => void; onDone: (text: string) => void }) {
  const open = item !== null;
  const returnFocus = useReturnFocus(open);
  const [last, setLast] = useState(item);
  if (item && item !== last) setLast(item);
  const shown = item ?? last;
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-md" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>Delete webhook {shown?.name}?</DialogTitle>
          <DialogDescription>Softwarden stops posting to it and deletes its address and secret. Its deliveries stay in the delivery log.</DialogDescription>
        </DialogHeader>
        {open && item && <DeleteConfirm key={item.id} item={item} onCancel={onClose} onDone={onDone} />}
      </DialogContent>
    </Dialog>
  );
}

/** Deletes the endpoint as read when the dialog opened (If-Match), so a change made meanwhile is shown, not lost. */
function DeleteConfirm({ item, onCancel, onDone }: { item: Webhook; onCancel: () => void; onDone: (text: string) => void }) {
  const loaded = useEndpointWithEtag(item.id);
  const [error, setError] = useState("");
  const [stale, setStale] = useState(false);
  const [busy, setBusy] = useState(false);
  async function remove() {
    setBusy(true);
    try {
      const etag = loaded.data?.etag;
      await typedApi("delete", "webhooks/{id}", { path: { id: item.id }, headers: etag ? { "If-Match": etag } : {} });
      onDone(`Webhook ${item.name} deleted. Its deliveries stay in the delivery log.`);
    } catch (e) {
      if (e instanceof Problem && e.status === 412 && e.type === PRECONDITION_FAILED) setStale(true);
      else setError(problemText(e, "Couldn't delete the webhook."));
    } finally {
      setBusy(false);
    }
  }
  if (loaded.error) return <ErrorBanner message={loaded.error} onRetry={loaded.reload} />;
  if (!loaded.data) return <Loading what="the webhook" />;
  return (
    <>
      {stale && (
        <div className="rounded-md border border-l-4 border-state-pending-border bg-state-pending px-4 py-3 text-state-pending-foreground" role="alert">
          <p className="font-semibold">Someone changed this webhook since you opened it. Nothing was deleted.</p>
          <div className="mt-2"><Button type="button" size="sm" variant="outline" onClick={() => { setStale(false); loaded.reload(); }}>Load the latest version</Button></div>
        </div>
      )}
      {error && <p className="rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2 text-sm" role="alert"><span aria-hidden="true">! </span>{error}</p>}
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        <Button type="button" variant="destructive" disabled={busy || loaded.fetching} onClick={() => void remove()}>{busy ? "Deleting…" : `Delete ${item.name}`}</Button>
      </DialogFooter>
    </>
  );
}
