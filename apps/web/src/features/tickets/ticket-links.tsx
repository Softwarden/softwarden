/** The Tickets section of an assessment or a renewal (capability `ticket_links`), loaded on first use.
 *  Each link: the system and the ticket's key, its title linked to the ticket, its status as a chip with a glyph and
 *  words (○ Open, ◐ In progress, ✓ Resolved, ● Closed, ? Unknown), the system's own status name, and when it was read
 *  or why the last read failed. Editors and Admins link a ticket by pasting its address (a live, debounced preview of
 *  the system and key the API parses it to, with its problems in words), change a link, remove it (a confirmation that
 *  names it) and read its status now. "Create ticket…" sends the title and description to Jira, ServiceNow or Halo; it
 *  isn't idempotent, so its button is disabled while the request runs and the dialog can't be dismissed, and a ticket
 *  created but not linkable (409 `not_linked`) is named so it can be linked by hand. Viewers read. */
import { useEffect, useRef, useState } from "react";
import { api, Problem, type RenewalCycle } from "@/api";
import { EmptyState, ErrorBanner, ErrorSummary, Field, Loading, Select, Textarea, useReturnFocus } from "@/components/app";
import { orgDate } from "@/components/org-banners";
import { chip, Panel, SuccessNote } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Unavailable } from "@/components/url-filters";
import type { Schemas } from "@/lib/openapi";
import { problemText, useApiQuery, useInvalidate } from "@/lib/query";
import { canWrite, useMe } from "@/lib/session";
import { OpenedEdit, type OpenedWrite } from "@/components/opened-edit";
import { ifMatch, isStaleWrite, staleDeleteText, useOpenedRecord } from "@/lib/if-match";
import { andList } from "@/lib/utils";

type TicketLink = Schemas["TicketLink"];
type SystemInfo = Schemas["TicketSystem"];
type System = Schemas["TicketConnectionSystemEnum"];
type Status = Schemas["TicketStatusEnum"];
export type TicketTarget = { kind: "assessment" | "renewal"; id: string; name: string; title: string; cycles?: RenewalCycle[] };

const SYSTEM_LABEL: Record<System, string> = { jira: "Jira", servicenow: "ServiceNow", halo: "HaloITSM" };
/** The ticket systems named in sentences: ServiceNow and Halo only while the installation turns preview features on
 *  (the `preview` capability), so a default install never names a system it doesn't offer. */
const namedSystems = (capabilities: string[]) => (capabilities.includes("preview") ? ["Jira", "ServiceNow", "Halo"] : ["Jira"]);
const STATUS: Record<Status, { glyph: string; text: string; tone: "unreviewed" | "pending" | "confirmed" | "neutral" }> = {
  open: { glyph: "○", text: "Open", tone: "unreviewed" },
  in_progress: { glyph: "◐", text: "In progress", tone: "pending" },
  resolved: { glyph: "✓", text: "Resolved", tone: "confirmed" },
  closed: { glyph: "●", text: "Closed", tone: "neutral" },
  unknown: { glyph: "?", text: "Unknown", tone: "neutral" },
};
/** The statuses an Editor sets by hand. Not Unknown: "Read it from the system" is that. */
const STATUS_CHOICES = (Object.keys(STATUS) as Status[]).filter((s) => s !== "unknown").map((s) => ({ value: s, label: STATUS[s].text }));

export function TicketStatusChip({ status, label }: { status: Status; label?: string }) {
  const s = STATUS[status] ?? { glyph: "?", text: label || status, tone: "neutral" as const };
  return <span className={chip({ state: s.tone })} data-ticket-status={status}><span aria-hidden="true">{s.glyph}</span>{s.text}</span>;
}

/** A link's name in sentences and buttons: its key, else its title, else its address's host. */
export function linkName(l: Pick<TicketLink, "external_key" | "title" | "url">) {
  if (l.external_key) return l.external_key;
  if (l.title) return l.title;
  try { return new URL(l.url).host; } catch { return l.url; }
}

/** A status an Editor set by hand. Unknown never counts: such a link is waiting to be read, or its read failed. */
const byHand = (l: Pick<TicketLink, "status_source" | "status">) => l.status_source === "manual" && l.status !== "unknown";

/** Whether a parsed ticket's status will be read automatically (GET ticket-links/systems), in words. A system the
 *  list leaves out (ServiceNow and Halo while the installation keeps preview features off) is never read here. */
function readsText(parsed: { system: string; system_label: string }, systems: SystemInfo[] | null) {
  const info = systems?.find((s) => s.system === parsed.system);
  if (!systems) return "";
  if (!info) return "Its status isn't read automatically: set it by hand below.";
  return info.reads
    ? `Its status is read automatically through the ${parsed.system_label} connection.`
    : `Its status isn't read automatically (no usable ${parsed.system_label} connection): set it by hand below.`;
}

const cycleLabel = (c: RenewalCycle) => `Cycle ${c.sequence}${c.renewal_date ? `, renews ${c.renewal_date}` : ""}`;
/** Field errors keyed by a dialog's input IDs, so the error summary's links reach them. */
const keyed = (prefix: string, errors: Record<string, string[]>) => Object.fromEntries(Object.entries(errors).map(([k, v]) => [`${prefix}-${k}`, v]));

const IconTicket = (
  <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M2 4.5h12v2a1.5 1.5 0 0 0 0 3v2H2v-2a1.5 1.5 0 0 0 0-3v-2Z" /><path d="M10 4.5v7" strokeDasharray="1 1.5" />
  </svg>
);

export function TicketLinks({ target }: { target: TicketTarget }) {
  const me = useMe();
  if (!me.capabilities.includes("ticket_links")) {
    return <Panel id="h-tickets" icon={IconTicket} title="Tickets"><div className="p-4"><Unavailable what="Ticket links" testId="tickets-unavailable" /></div></Panel>;
  }
  return <Tickets target={target} />;
}

function Tickets({ target }: { target: TicketTarget }) {
  const me = useMe();
  const write = canWrite(me);
  const base = `${target.kind === "assessment" ? "assessments" : "renewals"}/${target.id}/ticket-links`;
  const q = useApiQuery<TicketLink[]>(base);
  // What each system does here (every member may read it; only Editors and Admins need it, for Create ticket and the
  // link form's preview): Create ticket only where creating is on.
  const systems = useApiQuery<SystemInfo[]>("ticket-links/systems", { enabled: write });
  const creatable: System[] = (systems.data ?? []).filter((s) => s.creates).map((s) => s.system);
  /** Refresh is offered for a system the list names (once it has loaded): never for other tickets or a system this
   *  installation doesn't read (ServiceNow and Halo while preview features are off). */
  const readable = (system: string) => system !== "other" && (!systems.data || systems.data.some((s) => s.system === system));
  const invalidate = useInvalidate();
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [dialog, setDialog] = useState<{ mode: "add" | "create" } | { mode: "edit" | "remove"; link: TicketLink } | null>(null);
  const [refreshing, setRefreshing] = useState("");
  const links = q.data;
  const cycles = target.cycles ?? [];
  const cycleOf = (id: string | null) => cycles.find((c) => c.id === id);

  const reload = () => invalidate(base, target.kind === "assessment" ? `assessments/${target.id}` : `renewals/${target.id}`);
  /** The list reloads before the dialog closes, so a removed link's buttons are gone by then and focus goes to the
   *  dialog's fallback (Add a link), never the body. */
  async function done(text: string) {
    setError("");
    setNotice(text);
    await reload();
    setDialog(null);
  }
  async function refresh(l: TicketLink) {
    setRefreshing(l.id);
    setError("");
    setNotice("");
    try {
      const r = await api<TicketLink>(`${base}/${l.id}/refresh`, { method: "POST" });
      setNotice(r.status_error ? `Couldn't read ${linkName(r)}: ${r.status_error}` : `${linkName(r)} read: ${STATUS[r.status]?.text ?? r.status_label}${r.external_status ? ` (${r.external_status})` : ""}.`);
      void reload();
    } catch (e) {
      setError(`Couldn't read ${linkName(l)}. ${problemText(e, "Try again.")}`);
    } finally {
      setRefreshing("");
    }
  }
  const showCreate = write && creatable.length > 0;
  const action = write && (
    <span className="flex flex-wrap items-center gap-2">
      <Button id="tickets-add" size="sm" variant="outline" onClick={() => setDialog({ mode: "add" })}>Add a link</Button>
      {showCreate && (me.impersonation
        // The API refuses it while platform staff impersonate (an outward-facing write), so it isn't offered.
        ? <span className="text-sm text-muted-foreground" data-testid="create-off"><span aria-hidden="true">⊘ </span>Creating tickets is off while you act as this member.</span>
        : <Button id="tickets-create" size="sm" variant="outline" onClick={() => setDialog({ mode: "create" })}>Create ticket…</Button>)}
    </span>
  );
  return (
    <Panel id="h-tickets" icon={IconTicket} title="Tickets" count={links?.length} action={action}>
      <div className="px-4 pt-3">
        {write && systems.isError && (
          <ErrorBanner message={`Couldn't check which ticket systems are connected, so Create ticket isn't offered. ${problemText(systems.error, "Try again.")}`} onRetry={() => void systems.refetch()} />
        )}
        <SuccessNote text={notice} />
        {error && <ErrorBanner message={error} />}
      </div>
      {q.isError && <div className="px-4 pb-3"><ErrorBanner message={problemText(q.error, "Couldn't load the ticket links.")} onRetry={() => void q.refetch()} /></div>}
      {!links && !q.isError && <div className="px-4 pb-3"><Loading what="ticket links" rows={2} /></div>}
      {links && links.length === 0 && (
        <div className="px-4 pb-3">
          <EmptyState title="No tickets linked">
            {write ? "Paste a ticket's address with Add a link; its status is read where its system has a ticket connection, and set by hand otherwise." : `Editors and Admins link ${namedSystems(me.capabilities).join(", ")} and other tickets here.`}
          </EmptyState>
        </div>
      )}
      {links && links.length > 0 && (
        <ul className="pb-1" aria-label={`Tickets of ${target.name}`}>
          {links.map((l) => {
            const name = linkName(l);
            const cycle = cycleOf(l.cycle);
            return (
              <li key={l.id} className="border-t border-border-subtle px-4 py-3 first:border-t-0" data-ticket={name}>
                <div className="flex flex-wrap items-center gap-2">
                  <span className={chip({ state: "neutral" })} data-system={l.system}>{l.system_label}</span>
                  {l.external_key && <span className="font-mono text-sm font-semibold">{l.external_key}</span>}
                  <a href={l.url} target="_blank" rel="noreferrer noopener" className="text-link underline [overflow-wrap:anywhere]">
                    {l.title || (l.external_key ? "No title recorded" : l.url)}<span className="sr-only"> ({name}, opens in a new tab)</span>
                  </a>
                  <TicketStatusChip status={l.status} label={l.status_label} />
                  {write && (
                    <span className="ml-auto inline-flex flex-wrap gap-2">
                      {readable(l.system) && (
                        <Button size="sm" variant="outline" disabled={!!refreshing} onClick={() => void refresh(l)}>
                          {refreshing === l.id ? "Reading…" : "Refresh"}<span className="sr-only"> {name}</span>
                        </Button>
                      )}
                      <Button size="sm" variant="outline" onClick={() => setDialog({ mode: "edit", link: l })}>Edit<span className="sr-only"> {name}</span></Button>
                      <Button size="sm" variant="outline" onClick={() => setDialog({ mode: "remove", link: l })}>Remove<span className="sr-only"> {name}</span></Button>
                    </span>
                  )}
                </div>
                <p className="mt-1 text-sm text-muted-foreground" data-testid="ticket-read">
                  {l.external_status && <>{l.system_label} says “{l.external_status}”. </>}
                  {byHand(l)
                    ? "Status set by hand."
                    : l.status_source === "refreshed" && l.status_checked_at
                      ? <>Checked <span className="tabular">{orgDate(l.status_checked_at, me.organization.timezone)}</span>.</>
                      // Read once, then set to Unknown by hand: it has a check time, so it isn't waiting for a first read.
                      // (A failed first read has one too, with its error: that error is the line below.)
                      : l.status_source === "manual" && l.status_checked_at && !l.status_error
                        ? "Unknown (set by hand)."
                        // An unknown status waiting to be read: its failed read's error is the line below, else not read yet.
                        // A system never read here (other tickets, or ServiceNow and Halo while preview features are
                        // off) isn't waiting for anything: say so, so its status can be set by hand.
                        : l.status_error ? null : readable(l.system) ? "Not read yet." : "Not read here: set its status by hand."}
                  {cycle && <> About {cycleLabel(cycle).toLowerCase()}.</>}
                  {l.created_in_system && <> Created from Softwarden{l.created_by ? ` by ${l.created_by.name}` : ""}.</>}
                </p>
                {l.status_error && <p className="text-sm text-error-foreground" data-testid="ticket-error"><span aria-hidden="true">! </span>{l.status_error}</p>}
              </li>
            );
          })}
        </ul>
      )}
      {!write && links && <p className="border-t px-4 py-2 text-sm text-muted-foreground" data-testid="tickets-read-only">Editors and Admins add, change and refresh ticket links.</p>}
      {write && dialog && (dialog.mode === "add" || dialog.mode === "edit") && (
        <LinkDialog base={base} target={target} link={dialog.mode === "edit" ? dialog.link : null} systems={systems.data ?? null} onClose={() => setDialog(null)} onDone={done} />
      )}
      {write && dialog?.mode === "remove" && <RemoveDialog base={base} link={dialog.link} onClose={() => setDialog(null)} onDone={done} />}
      {write && dialog?.mode === "create" && <CreateDialog base={base} target={target} systems={creatable} onClose={() => setDialog(null)} onDone={done} />}
    </Panel>
  );
}

// ---- link or change a ticket ------------------------------------------------------------------------------------------

type Parsed = { system: string; system_label: string; external_key: string };

/** The system and key an address parses to, asked of the API a moment after typing stops. */
function useParse(url: string, unchanged: boolean) {
  const [state, setState] = useState<{ url: string; parsed: Parsed | null; problem: string; busy: boolean }>({ url: "", parsed: null, problem: "", busy: false });
  useEffect(() => {
    const value = url.trim();
    if (!value || unchanged) { setState({ url: value, parsed: null, problem: "", busy: false }); return; }
    let live = true;
    setState((s) => ({ ...s, busy: true }));
    const t = setTimeout(() => {
      api<Parsed>(`ticket-links/parse?url=${encodeURIComponent(value)}`)
        .then((parsed) => { if (live) setState({ url: value, parsed, problem: "", busy: false }); })
        .catch((e) => {
          if (!live) return;
          const problem = e instanceof Problem && e.errors.url ? e.errors.url.join(" ") : problemText(e, "Couldn't check the address.");
          setState({ url: value, parsed: null, problem, busy: false });
        });
    }, 350);
    return () => { live = false; clearTimeout(t); };
  }, [url, unchanged]);
  return state;
}

function LinkDialog({ base, target, link, systems, onClose, onDone }: {
  base: string; target: TicketTarget; link: TicketLink | null; systems: SystemInfo[] | null; onClose: () => void; onDone: (text: string) => Promise<void>;
}) {
  const returnFocus = useReturnFocus(true, () => document.getElementById("tickets-add"));
  const form = { base, target, systems, onClose, onDone };
  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose(); }}>
      <DialogContent className="max-w-lg" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>{link ? `Change the link to ${linkName(link)}` : `Link a ticket to ${target.name}`}</DialogTitle>
          <DialogDescription>Paste the ticket's address as the browser shows it. Softwarden reads the status through the system's ticket connection, never through this address.</DialogDescription>
        </DialogHeader>
        {/* A change starts from the link read with its ETag, which the save sends as If-Match. */}
        {link
          ? <OpenedEdit<TicketLink> path={`${base}/${link.id}`} what="ticket link">{(fresh, write) => <LinkForm {...form} link={fresh} write={write} />}</OpenedEdit>
          : <LinkForm {...form} link={null} />}
      </DialogContent>
    </Dialog>
  );
}

function LinkForm({ base, target, link, write, systems, onClose, onDone }: {
  base: string; target: TicketTarget; link: TicketLink | null; write?: OpenedWrite; systems: SystemInfo[] | null; onClose: () => void; onDone: (text: string) => Promise<void>;
}) {
  const me = useMe();
  const [url, setUrl] = useState(link?.url ?? "");
  const [title, setTitle] = useState(link?.title ?? "");
  const [status, setStatus] = useState<Status | "">(link && byHand(link) ? link.status : "");
  const [cycle, setCycle] = useState(link?.cycle ?? "");
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [failure, setFailure] = useState("");
  const [busy, setBusy] = useState(false);
  const parse = useParse(url, !!link && url.trim() === link.url);
  const cycles = target.cycles ?? [];

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    if (!url.trim()) { setErrors({ "tl-url": ["Paste the ticket's address."] }); return; }
    const body: Record<string, unknown> = {};
    if (!link || url.trim() !== link.url) body.url = url.trim();
    if (!link || title.trim() !== link.title) body.title = title.trim();
    if (status && (!link || !byHand(link) || status !== link.status)) body.status = status;
    // Back from a status set by hand to reading it from the system: null makes it unknown until the next read.
    if (!status && link && byHand(link)) body.status = null;
    if (target.kind === "renewal" && (!link || (cycle || null) !== link.cycle)) body.cycle = cycle || null;
    if (link && Object.keys(body).length === 0) { await onDone("Nothing changed."); return; }
    setBusy(true); setErrors({}); setFailure("");
    try {
      const saved = await api<TicketLink>(link ? `${base}/${link.id}` : base, { method: link ? "PATCH" : "POST", body, headers: link ? await write?.ifMatch() : undefined });
      await onDone(link ? `${linkName(saved)} saved.` : `Linked ${saved.system_label} ticket ${linkName(saved)}.`);
    } catch (err) {
      if (isStaleWrite(err) && write) write.onStale();
      else if (err instanceof Problem && Object.keys(err.errors).length) setErrors(keyed("tl", err.errors));
      else setFailure(`${problemText(err, "Couldn't save the link.")} Nothing was saved.`);
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <form id="tl-form" onSubmit={(e) => void save(e)} noValidate aria-label="Ticket link">
        {failure && <ErrorBanner message={failure} />}
        <ErrorSummary errors={errors} focusOnShow="field" />
        <Field id="tl-url" label="Ticket address (required)" errors={errors} help="Such as https://yourcompany.atlassian.net/browse/ITOPS-12.">
          <Input id="tl-url" type="url" inputMode="url" autoComplete="off" spellCheck={false} value={url} onChange={(e) => setUrl(e.target.value)} />
        </Field>
        <p className="-mt-2 mb-3 min-h-5 text-sm" aria-live="polite" data-testid="parse-preview">
          {parse.busy ? <span className="text-muted-foreground">Checking the address…</span>
            : parse.problem ? <span className="text-error-foreground"><span aria-hidden="true">! </span>{parse.problem}</span>
              : parse.parsed ? (parse.parsed.system === "other"
                ? <span className="text-muted-foreground"><span aria-hidden="true">○ </span>Not a {andList(namedSystems(me.capabilities), "or")} address: linked as another ticket, with its status set by hand.</span>
                : <>
                    <span className="block"><span aria-hidden="true">✓ </span>{parse.parsed.system_label} ticket <span className="font-mono font-semibold">{parse.parsed.external_key}</span></span>
                    <span className="block text-muted-foreground" data-testid="parse-reads">{readsText(parse.parsed, systems)}</span>
                  </>)
                : null}
        </p>
        <Field id="tl-title" label="Title" errors={errors} help="Optional; shown with the link.">
          <Input id="tl-title" value={title} maxLength={500} onChange={(e) => setTitle(e.target.value)} />
        </Field>
        <Field id="tl-status" label="Status" errors={errors} help="Set it by hand for a ticket Softwarden can't read. A later read through the ticket connection replaces it.">
          <Select id="tl-status" value={status} onChange={(e) => setStatus(e.target.value as Status | "")}>
            <option value="">Read it from the system</option>
            {STATUS_CHOICES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
          </Select>
        </Field>
        {target.kind === "renewal" && cycles.length > 0 && (
          <Field id="tl-cycle" label="Cycle" errors={errors}>
            <Select id="tl-cycle" value={cycle} onChange={(e) => setCycle(e.target.value)}>
              <option value="">The renewal as a whole</option>
              {cycles.map((c) => <option key={c.id} value={c.id}>{cycleLabel(c)}</option>)}
            </Select>
          </Field>
        )}
      </form>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
        <Button type="submit" form="tl-form" disabled={busy}>{busy ? "Saving…" : link ? "Save link" : "Link ticket"}</Button>
      </DialogFooter>
    </>
  );
}

function RemoveDialog({ base, link, onClose, onDone }: { base: string; link: TicketLink; onClose: () => void; onDone: (text: string) => Promise<void> }) {
  const returnFocus = useReturnFocus(true, () => document.getElementById("tickets-add"));
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const name = linkName(link);
  // Read with its ETag as the confirmation opens; the removal sends it as If-Match.
  const opened = useOpenedRecord(`${base}/${link.id}`);
  async function remove() {
    setBusy(true); setError("");
    try {
      await api(`${base}/${link.id}`, { method: "DELETE", headers: ifMatch(await opened.etag()) });
      await onDone(`Removed the link to ${name}.`);
    } catch (e) {
      setError(isStaleWrite(e) ? staleDeleteText("link", "remove") : `${problemText(e, "Couldn't remove it.")} Nothing was changed.`);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose(); }}>
      <DialogContent className="max-w-md" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>Remove the link to {name}?</DialogTitle>
          <DialogDescription>Only the link goes. The ticket itself stays as it is in {link.system_label}.</DialogDescription>
        </DialogHeader>
        {error && <ErrorBanner message={error} />}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
          <Button type="button" variant="destructive" disabled={busy} onClick={() => void remove()}>{busy ? "Removing…" : `Remove ${name}`}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ---- create a ticket ---------------------------------------------------------------------------------------------------

function CreateDialog({ base, target, systems, onClose, onDone }: {
  base: string; target: TicketTarget; systems: System[]; onClose: () => void; onDone: (text: string) => Promise<void>;
}) {
  const returnFocus = useReturnFocus(true, () => document.getElementById("tickets-create") ?? document.getElementById("tickets-add"));
  const [system, setSystem] = useState<System>(systems[0] ?? "jira");
  const [title, setTitle] = useState(target.title);
  const [description, setDescription] = useState("");
  const [cycle, setCycle] = useState("");
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [failure, setFailure] = useState("");
  const [orphan, setOrphan] = useState<{ key: string; system: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const running = useRef(false);
  const label = SYSTEM_LABEL[system];
  const what = target.kind === "assessment" ? "assessment" : "renewal";

  async function create(e: React.FormEvent) {
    e.preventDefault();
    if (running.current || orphan) return; // never twice: a second request would make a second ticket
    if (!title.trim()) { setErrors({ "ct-title": ["Enter the ticket's title."] }); return; }
    running.current = true;
    setBusy(true); setErrors({}); setFailure("");
    const body: Record<string, unknown> = { system, title: title.trim() };
    if (description.trim()) body.description = description.trim();
    if (target.kind === "renewal" && cycle) body.cycle = cycle;
    try {
      const link = await api<TicketLink>(`${base}/create-ticket`, { method: "POST", body });
      await onDone(`Created ${label} ticket ${linkName(link)} and linked it.`);
    } catch (err) {
      if (err instanceof Problem && err.status === 409 && err.body.problem === "not_linked") {
        setOrphan({ key: String(err.body.external_key ?? ""), system: label });
      } else if (err instanceof Problem && Object.keys(err.errors).length) {
        setErrors(keyed("ct", err.errors));
      } else {
        setFailure(problemText(err, `Couldn't create the ${label} ticket.`));
      }
    } finally {
      running.current = false;
      setBusy(false);
    }
  }
  return (
    <Dialog open onOpenChange={(o) => { if (!o && !running.current) onClose(); }}>
      <DialogContent className="max-w-lg" {...returnFocus}
        onEscapeKeyDown={(e) => { if (running.current) e.preventDefault(); }} onInteractOutside={(e) => { if (running.current) e.preventDefault(); }}>
        <DialogHeader>
          <DialogTitle>Create a ticket for {target.name}</DialogTitle>
          <DialogDescription>A new ticket is created in the system you choose and linked here. Softwarden can't delete it there afterwards.</DialogDescription>
        </DialogHeader>
        {orphan ? (
          <div className="rounded-md border border-l-4 border-state-pending-border bg-state-pending px-3 py-2 text-state-pending-foreground" role="alert" data-testid="not-linked">
            <p className="font-semibold">The {orphan.system} ticket <code className="font-mono">{orphan.key || "(no key given)"}</code> was created, but couldn't be linked here.</p>
            <p>Link it by hand: open {orphan.key || "it"} in {orphan.system}, copy its address and use Add a link. Don't create it again.</p>
          </div>
        ) : (
          <form id="ct-form" onSubmit={(e) => void create(e)} noValidate aria-label="New ticket">
            {failure && <ErrorBanner message={failure} />}
            <ErrorSummary errors={errors} focusOnShow="field" />
            <fieldset disabled={busy}>
              <Field id="ct-system" label="Create it in" errors={errors}>
                <Select id="ct-system" value={system} onChange={(e) => setSystem(e.target.value as System)}>
                  {systems.map((s) => <option key={s} value={s}>{SYSTEM_LABEL[s]}</option>)}
                </Select>
              </Field>
              <Field id="ct-title" label="Title (required)" errors={errors}>
                <Input id="ct-title" value={title} maxLength={255} onChange={(e) => setTitle(e.target.value)} />
              </Field>
              <Field id="ct-description" label="Description" errors={errors} help={`Leave empty to send one line linking back to this ${what} in Softwarden.`}>
                <Textarea id="ct-description" value={description} maxLength={5000} onChange={(e) => setDescription(e.target.value)} />
              </Field>
              {target.kind === "renewal" && (target.cycles ?? []).length > 0 && (
                <Field id="ct-cycle" label="Cycle" errors={errors}>
                  <Select id="ct-cycle" value={cycle} onChange={(e) => setCycle(e.target.value)}>
                    <option value="">The renewal as a whole</option>
                    {(target.cycles ?? []).map((c) => <option key={c.id} value={c.id}>{cycleLabel(c)}</option>)}
                  </Select>
                </Field>
              )}
            </fieldset>
            <div className="rounded-md border bg-muted px-3 py-2 text-sm" data-testid="create-sends">
              <p className="font-semibold">What is sent to {label}</p>
              <p>The title “{title.trim() || "…"}” and {description.trim() ? "the description above" : `a line linking back to this ${what}`}. Nothing else.</p>
            </div>
          </form>
        )}
        <DialogFooter>
          {orphan
            ? <Button type="button" onClick={onClose}>Done</Button>
            : <>
                <Button type="button" variant="outline" disabled={busy} onClick={onClose}>Cancel</Button>
                <Button type="submit" form="ct-form" disabled={busy}>{busy ? `Creating in ${label}…` : `Create the ticket in ${label}`}</Button>
              </>}
        </DialogFooter>
        <p className="sr-only" aria-live="polite">{busy ? `Creating the ticket in ${label}. Don't close this dialog.` : ""}</p>
      </DialogContent>
    </Dialog>
  );
}
