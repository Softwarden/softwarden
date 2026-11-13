/** Reports: `/reports`, loaded on first use. Tabs in the URL
 *  (`tab`):
 *
 *  - Spend (`contracts`): the annualised cost of live contracts per currency, grouped by vendor, department, category or
 *    cost centre (`by`), with the allocation detail (one row per contract line and each contract's unallocated rest),
 *    and its CSV.
 *  - Renewals calendar: every open action deadline for the next 1 to 24 months (`months`), overdue first, counted per
 *    month, its CSV, and the personal calendar feed (an ICS address with a secret, shown once, for Outlook, Google or
 *    Apple Calendar; create, rotate, revoke).
 *  - Licence utilisation (`contracts`): seats per application and SKU, and its CSV.
 *
 *  Every role reads them; exports are recorded in the audit log. No charts, so Recharts isn't loaded here. */
import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { api, Problem, when } from "@/api";
import { AllClear, EmptyState, ErrorBanner, Field, Loading, PageHead, Select, TabStrip, TableRegion, useReturnFocus } from "@/components/app";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { OverusedChip, UtilisationMeter } from "@/features/money/contract-ui";
import { percentText } from "@/features/money/contract-types";
import { useDownload } from "@/lib/download";
import { apiKey, problemText, useApiQuery } from "@/lib/query";
import { recordPath } from "@/lib/kinds";
import { useMe } from "@/lib/session";
import { monthName, SPEND_BY, type CalendarFeed, type CalendarFeedCreated, type CalendarReport, type PoolTotalsRow, type SpendBy, type SpendReport, type Utilisation } from "./report-types";
import { Amount, Block, CommitmentTiles, DeadlinesTable, GroupName, SpendGroupsTable } from "./report-ui";

type Tab = "spend" | "calendar" | "licences";

export function ReportsPage() {
  const me = useMe();
  const contracts = me.capabilities.includes("contracts");
  const [params, setParams] = useSearchParams();
  const tabs: { id: Tab; label: string }[] = [
    ...(contracts ? [{ id: "spend" as const, label: "Spend" }] : []),
    { id: "calendar", label: "Renewals calendar" },
    ...(contracts ? [{ id: "licences" as const, label: "License utilization" }] : []),
  ];
  const asked = params.get("tab");
  const tab: Tab = tabs.some((t) => t.id === asked) ? (asked as Tab) : tabs[0].id;
  // Each tab keeps only its own parameters, so a shared link says exactly what it shows.
  const select = (t: Tab) => setParams(t === tabs[0].id ? {} : { tab: t });
  return (
    <>
      {/* Each tab has its own document title, so tabs, history and screen readers tell them apart. */}
      <PageHead title="Reports" docTitle={`${tabs.find((t) => t.id === tab)!.label} · Reports`} desc={`Spend, the renewals calendar and license utilization for ${me.organization.name}, each with a CSV export.`} />
      {!contracts && (
        <p className="mb-4 max-w-prose text-sm text-muted-foreground"><span aria-hidden="true">⊘ </span>Spend and license utilization aren't available: this organization's edition doesn't include contracts.</p>
      )}
      <TabStrip label="Reports" tabs={tabs} tab={tab} onSelect={select} />
      <div role="tabpanel" id="tabpanel" aria-labelledby={`tab-${tab}`}>
        {tab === "spend" && <SpendTab />}
        {tab === "calendar" && <CalendarTab />}
        {tab === "licences" && <LicencesTab />}
      </div>
    </>
  );
}

/** An export button with its error under it and a status line once the file is saved. */
function ExportButton({ path, fallback, what }: { path: string; fallback: string; what: string }) {
  const d = useDownload();
  const [saved, setSaved] = useState("");
  return (
    <div className="flex flex-col items-end gap-1">
      <Button type="button" variant="outline" disabled={d.busy}
        onClick={() => { setSaved(""); void d.download(path, fallback).then((name) => name && setSaved(`Downloaded ${name}. The export is recorded in the audit log.`)); }}>
        {d.busy ? "Preparing CSV…" : "Export CSV"}<span className="sr-only"> of {what}</span>
      </Button>
      <p className="text-sm text-muted-foreground" role="status">{saved}</p>
      {d.error && <p className="text-sm text-error-foreground" role="alert">{d.error}</p>}
    </div>
  );
}

// ---- spend ------------------------------------------------------------------------------------------------------------

function SpendTab() {
  const [params, setParams] = useSearchParams();
  const raw = params.get("by");
  const by: SpendBy = SPEND_BY.some((b) => b.id === raw) ? (raw as SpendBy) : "vendor";
  const noun = SPEND_BY.find((b) => b.id === by)!.noun;
  const q = useApiQuery<SpendReport>(`reports/spend?by=${by}`);
  const setBy = (b: SpendBy) => setParams(b === "vendor" ? { tab: "spend" } : { tab: "spend", by: b });
  const r = q.data;
  return (
    <>
      <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
        <p className="max-w-prose text-sm text-muted-foreground">
          The annualized cost of Active and Pending renewal contracts, split over their lines to the cent and grouped per currency. Every grouping adds up to the same total; cost with no {noun} is a named gap.
        </p>
        <ExportButton path={`reports/spend/export.csv?by=${by}`} fallback={`softwarden-spend-by-${by.replace("_", "-")}.csv`} what={`spend by ${noun}`} />
      </div>
      <TabStrip idPrefix="by-" label="Group spend by" tabs={SPEND_BY.map((b) => ({ id: b.id, label: `By ${b.noun}` }))} tab={by} onSelect={setBy} />
      <div role="tabpanel" id="by-tabpanel" aria-labelledby={`by-tab-${by}`}>
        {q.isError && <ErrorBanner message={`${problemText(q.error, "Couldn't load the spend report.")} Nothing was changed.`} onRetry={() => void q.refetch()} />}
        {q.isPending && <Loading what="the spend report" rows={4} />}
        {r && r.totals.length === 0 && (
          <EmptyState title="No spend yet" action={<Button asChild variant="outline"><Link to="/contracts">Open Contracts</Link></Button>}>
            Spend counts Active and Pending renewal contracts with an amount. Signed contracts count once they start.
          </EmptyState>
        )}
        {r && r.totals.map((t) => {
          const rows = r.rows.filter((g) => g.currency === t.currency);
          const lines = r.allocations.filter((a) => a.currency === t.currency);
          const id = `h-spend-${t.currency.toLowerCase()}`;
          return (
            <section key={t.currency} aria-labelledby={id} className="mb-6 grid gap-4" data-currency={t.currency}>
              <h2 id={id} className="font-display text-section font-semibold">Spend in {t.currency}</h2>
              <CommitmentTiles c={t} idPrefix="spend" />
              <div className="overflow-hidden rounded-lg border bg-card">
                <SpendGroupsTable rows={rows} currency={t.currency} by={noun} label={`Spend in ${t.currency} by ${noun} table`} />
              </div>
              <Block id={`${id}-detail`} title={`Allocation detail, ${t.currency}`}>
                <TableRegion label={`Allocation detail in ${t.currency} table`} className="rounded-none border-0">
                  <Table>
                    <TableCaption className="sr-only">Each contract line's share of its contract's annualized cost in {t.currency}, and the {noun} it counts under</TableCaption>
                    <TableHeader><TableRow>
                      <TableHead scope="col">Contract</TableHead><TableHead scope="col">Line</TableHead>
                      <TableHead scope="col" aria-sort="ascending">{noun.charAt(0).toUpperCase() + noun.slice(1)}</TableHead>
                      <TableHead scope="col" className="text-right">Share</TableHead><TableHead scope="col" className="text-right">Annualized</TableHead>
                      <TableHead scope="col">Cost center</TableHead>
                    </TableRow></TableHeader>
                    <TableBody>
                      {lines.map((a, i) => (
                        <TableRow key={`${a.contract.id}-${a.record?.id ?? "rest"}-${i}`} data-gap={a.gap ?? ""}>
                          <TableHead scope="row" className="whitespace-normal font-normal text-foreground">
                            <Link to={`/contracts/${a.contract.id}`} className="inline-block min-h-6 leading-6">{a.contract.name}</Link>
                            {a.vendor && <span className="block text-sm text-muted-foreground">{a.vendor.name}</span>}
                          </TableHead>
                          <TableCell className="whitespace-normal">
                            {a.record ? (recordPath(a.record.kind, a.record.id) ? <Link to={recordPath(a.record.kind, a.record.id)!} className="inline-block min-h-6 leading-6">{a.record.name}</Link> : a.record.name) : <span className="italic">Not allocated</span>}
                          </TableCell>
                          <TableCell className="whitespace-normal"><GroupName g={a} /></TableCell>
                          <TableCell className="tabular text-right">{percentText(a.percent)}%</TableCell>
                          <TableCell className="text-right"><Amount amount={a.amount} currency={a.currency} /></TableCell>
                          <TableCell>{a.cost_center || <span className="text-muted-foreground">—</span>}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </TableRegion>
              </Block>
            </section>
          );
        })}
      </div>
    </>
  );
}

// ---- renewals calendar ------------------------------------------------------------------------------------------------

const MONTH_CHOICES = [1, 2, 3, 6, 9, 12, 18, 24];

function CalendarTab() {
  const [params, setParams] = useSearchParams();
  const raw = Number(params.get("months"));
  const months = Number.isInteger(raw) && raw >= 1 && raw <= 24 ? raw : 12;
  const q = useApiQuery<CalendarReport>(`reports/renewals-calendar?months=${months}`);
  const setMonths = (m: number) => setParams(m === 12 ? { tab: "calendar" } : { tab: "calendar", months: String(m) });
  const r = q.data;
  const overdue = r?.rows.filter((x) => x.timing.overdue) ?? [];
  const upcoming = r?.rows.filter((x) => !x.timing.overdue) ?? [];
  return (
    <div className="grid gap-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="w-56">
          <Field id="calendar-months" label="Months ahead" help="This month first.">
            <Select id="calendar-months" value={String(months)} onChange={(e) => setMonths(Number(e.target.value))}>
              {[...new Set([...MONTH_CHOICES, months])].sort((a, b) => a - b).map((m) => <option key={m} value={m}>{m} {m === 1 ? "month" : "months"}</option>)}
            </Select>
          </Field>
        </div>
        <ExportButton path={`reports/renewals-calendar/export.csv?months=${months}`} fallback="softwarden-renewals-calendar.csv" what="the renewals calendar" />
      </div>
      {q.isError && <ErrorBanner message={`${problemText(q.error, "Couldn't load the renewals calendar.")} Nothing was changed.`} onRetry={() => void q.refetch()} />}
      {q.isPending && <Loading what="the renewals calendar" rows={4} />}
      {r && (
        <>
          <p className="text-sm text-muted-foreground" role="status">{r.rows.length} open {r.rows.length === 1 ? "deadline" : "deadlines"} from {r.from} to {r.to}{r.overdue ? `, ${r.overdue} overdue` : ""}. The dates are the ones reminders use.</p>
          <Block id="h-overdue" title={`Overdue (${r.overdue})`} testId="calendar-overdue">
            {overdue.length === 0
              ? <div className="p-4"><AllClear>Nothing is overdue.</AllClear></div>
              : <DeadlinesTable rows={overdue} label="Overdue deadlines table" caption="Overdue action deadlines, oldest first" />}
          </Block>
          <Block id="h-by-month" title="By month">
            <TableRegion label="Deadlines by month table" className="rounded-none border-0">
              <Table>
                <TableCaption className="sr-only">Open action deadlines per month, this month first; overdue ones count in their own month</TableCaption>
                <TableHeader><TableRow><TableHead scope="col" aria-sort="ascending">Month</TableHead><TableHead scope="col" className="text-right">Deadlines</TableHead></TableRow></TableHeader>
                <TableBody>
                  {r.by_month.map((m) => (
                    <TableRow key={m.month} data-month={m.month}>
                      <TableHead scope="row" className="font-normal text-foreground">{monthName(m.month)}</TableHead>
                      <TableCell className="tabular text-right">{m.count}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableRegion>
          </Block>
          <Block id="h-upcoming" title={`Coming up (${upcoming.length})`}>
            {upcoming.length === 0
              ? <div className="p-4"><EmptyState title="No deadlines in this window">Renewals with an open cycle and contracts with a cancellation deadline show here. Choose more months to look further ahead.</EmptyState></div>
              : <DeadlinesTable rows={upcoming} label="Upcoming deadlines table" caption="Open action deadlines in the window, soonest first" />}
          </Block>
        </>
      )}
      <CalendarFeedPanel />
    </div>
  );
}

// ---- calendar feed ----------------------------------------------------------------------------------------------------

/** The person's own subscription to the renewals calendar. The address holds a secret, so the API shows it once, when
 *  it is created or rotated; afterwards only its prefix, when it was made and when a calendar last fetched it. */
function CalendarFeedPanel() {
  const client = useQueryClient();
  const feed = useApiQuery<CalendarFeed>("calendar-feed");
  const [shown, setShown] = useState<{ url: string; rotated: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [dialog, setDialog] = useState<"rotate" | "revoke" | null>(null);
  const refresh = () => client.invalidateQueries({ queryKey: apiKey("calendar-feed") });
  // Where focus goes once the panel shows the result: the new address after create or rotate, "Create calendar feed"
  // after revoke. The button that was focused is gone by then, and focus must not fall to the page body.
  // `dialog`: the dialog's close places it (Radix moves focus when its close finishes, after any effect here).
  const pendingFocus = useRef<{ id: string; dialog: boolean } | null>(null);
  const placeFocus = () => {
    const id = pendingFocus.current?.id;
    const el = id ? document.getElementById(id) : null;
    if (el) { el.focus(); pendingFocus.current = null; return true; }
    return false;
  };

  async function create() {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const made = await api<CalendarFeedCreated>("calendar-feed", { method: "POST" });
      pendingFocus.current = { id: "feed-url", dialog: false };
      setShown({ url: made.url, rotated: false });
      setNotice("Calendar feed created. Copy its address into your calendar app now.");
    } catch (e) {
      setError(e instanceof Problem && e.status === 409
        ? "You already have a calendar feed (made in another tab or browser). Its address can't be shown again; rotate it to get a new one."
        : `${problemText(e, "Couldn't create the calendar feed.")} Nothing was created.`);
    } finally {
      setBusy(false);
      void refresh();
    }
  }
  async function rotate() {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const made = await api<CalendarFeedCreated>("calendar-feed/rotate", { method: "POST" });
      pendingFocus.current = { id: "feed-url", dialog: true };
      setShown({ url: made.url, rotated: true });
      setNotice("New address made. The old one has stopped working; subscribe again with this one.");
      setDialog(null);
    } catch (e) {
      setError(`${problemText(e, "Couldn't rotate the calendar feed.")} The current address still works.`);
      setDialog(null);
    } finally {
      setBusy(false);
      void refresh();
    }
  }
  async function revoke() {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await api("calendar-feed", { method: "DELETE" });
      pendingFocus.current = { id: "feed-create", dialog: true };
      setShown(null);
      setNotice("Calendar feed revoked. Calendars subscribed to it stop updating.");
      setDialog(null);
    } catch (e) {
      setError(`${problemText(e, "Couldn't revoke the calendar feed.")} It still works.`);
      setDialog(null);
    } finally {
      setBusy(false);
      void refresh();
    }
  }

  const f = feed.data;
  // The target appears after a render (the address) or after the refetch (Create); focus it then.
  useEffect(() => { if (pendingFocus.current && !pendingFocus.current.dialog) placeFocus(); });
  /** A dialog closing after its action: focus the result rather than the opener, or the heading until it appears. */
  const afterDialog = (e: Event) => {
    if (!pendingFocus.current?.dialog) return false;
    e.preventDefault();
    if (!placeFocus()) {
      // Not there yet (Create appears after the refetch): hold focus on the heading, then the effect moves it.
      document.getElementById("h-feed")?.focus();
      pendingFocus.current = { ...pendingFocus.current, dialog: false };
    }
    return true;
  };
  return (
    <Block id="h-feed" title="Calendar subscription" testId="calendar-feed">
      <div className="grid gap-4 p-4">
        <p className="max-w-prose text-sm text-muted-foreground">
          Your own address for these deadlines as a calendar (one all-day event per deadline, updated when the dates change), for Outlook, Google Calendar or Apple Calendar.
          It reads as you in this organization and works without signing in, so treat it like a password. It stops when you revoke or rotate it, or leave the organization.
        </p>
        <p className="sr-only" role="status">{notice}</p>
        {notice && <p className="rounded-md border border-success-border bg-success-bg px-3 py-2 text-success-foreground" aria-hidden="true"><span>✓ </span>{notice}</p>}
        {error && <ErrorBanner message={error} />}
        {feed.isError && <ErrorBanner message={`${problemText(feed.error, "Couldn't load your calendar feed.")}`} onRetry={() => void feed.refetch()} />}
        {feed.isPending && <Loading what="your calendar feed" />}
        {shown && <FeedAddress url={shown.url} rotated={shown.rotated} />}
        {f && !f.active && !shown && (
          <div>
            <p className="mb-2">You have no calendar feed.</p>
            <Button type="button" id="feed-create" variant="outline" disabled={busy} onClick={() => void create()}>{busy ? "Creating…" : "Create calendar feed"}</Button>
          </div>
        )}
        {f && f.active && (
          <div className="grid gap-3">
            <dl className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1 text-sm" data-testid="feed-facts">
              <dt className="text-muted-foreground">Address</dt><dd><span className="font-mono">{f.prefix}…</span> <span className="text-muted-foreground">(the rest is secret and isn't kept)</span></dd>
              <dt className="text-muted-foreground">Created</dt><dd className="tabular">{when(f.created_at)}</dd>
              <dt className="text-muted-foreground">Last fetched</dt><dd className="tabular" data-testid="feed-last-used">{f.last_used_at ? when(f.last_used_at) : "Not yet: no calendar has fetched it"}</dd>
            </dl>
            <div className="flex flex-wrap gap-2">
              <Button type="button" variant="outline" disabled={busy} onClick={() => setDialog("rotate")}>Rotate address…</Button>
              <Button type="button" variant="outline" disabled={busy} onClick={() => setDialog("revoke")}>Revoke…</Button>
            </div>
            <p className="max-w-prose text-sm text-muted-foreground">Lost the address? It can't be shown again: rotate the feed to get a new one.</p>
          </div>
        )}
        <SubscribeHelp />
      </div>
      <FeedDialog open={dialog === "rotate"} onClose={() => setDialog(null)} afterClose={afterDialog} title="Rotate your calendar feed address?"
        description="A new address replaces the current one. The current address stops working at once: every calendar subscribed to it stops updating until you subscribe again with the new address."
        confirm={busy ? "Rotating…" : "Rotate address"} busy={busy} onConfirm={() => void rotate()} />
      <FeedDialog open={dialog === "revoke"} onClose={() => setDialog(null)} afterClose={afterDialog} title="Revoke your calendar feed?"
        description="The address stops working at once and calendars subscribed to it stop updating. You can create a new feed later."
        confirm={busy ? "Revoking…" : "Revoke feed"} busy={busy} onConfirm={() => void revoke()} />
    </Block>
  );
}

function FeedDialog({ open, onClose, afterClose, title, description, confirm, busy, onConfirm }: {
  afterClose: (e: Event) => boolean;
  open: boolean; onClose: () => void; title: string; description: string; confirm: string; busy: boolean; onConfirm: () => void;
}) {
  const returnFocus = useReturnFocus(open);
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg" onCloseAutoFocus={(e) => { if (!afterClose(e)) returnFocus.onCloseAutoFocus(e); }}>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
          <Button type="button" variant="destructive" disabled={busy} onClick={onConfirm}>{confirm}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** The address, once: a read-only box (selected on focus) and Copy. */
function FeedAddress({ url, rotated }: { url: string; rotated: boolean }) {
  const [copied, setCopied] = useState("");
  async function copy() {
    try {
      await navigator.clipboard.writeText(url);
      setCopied("Copied the address.");
    } catch {
      const box = document.getElementById("feed-url") as HTMLInputElement | null;
      box?.focus();
      box?.select();
      setCopied("Couldn't copy automatically: the address is selected, press Ctrl+C (Cmd+C on a Mac) to copy it.");
    }
  }
  return (
    <div className="rounded-md border border-l-4 border-state-pending-border bg-state-pending px-4 py-3 text-state-pending-foreground" data-testid="feed-address">
      <p className="font-semibold"><span aria-hidden="true">! </span>Copy this address now: it is shown only once.{rotated && " The previous address no longer works."}</p>
      <div className="mt-2 flex flex-wrap items-end gap-2">
        <div className="flex min-w-0 flex-1 flex-col gap-1.5">
          <label htmlFor="feed-url" className="text-sm font-medium">Calendar feed address</label>
          <Input id="feed-url" readOnly value={url} className="bg-card font-mono text-foreground" onFocus={(e) => e.currentTarget.select()} />
        </div>
        <Button type="button" variant="outline" onClick={() => void copy()}>Copy address</Button>
      </div>
      <p className="mt-1 text-sm" role="status">{copied}</p>
    </div>
  );
}

function SubscribeHelp() {
  return (
    <section aria-labelledby="h-subscribe" className="grid gap-3 border-t pt-4 text-sm">
      <h3 id="h-subscribe" className="font-display text-section font-semibold">How to subscribe</h3>
      <p className="max-w-prose text-muted-foreground">Paste the https address as it is. Calendar apps fetch it every few hours, so a changed date can take a while to show.</p>
      <div className="grid gap-4 md:grid-cols-3">
        <div>
          <h4 className="font-semibold">Outlook</h4>
          <ol className="ml-5 list-decimal">
            <li>Open Calendar in Outlook on the web or the new Outlook.</li>
            <li>Choose <strong>Add calendar</strong>, then <strong>Subscribe from web</strong>.</li>
            <li>Paste the address, name the calendar (for example "Softwarden renewals") and choose <strong>Import</strong>.</li>
          </ol>
          <p className="mt-1 text-muted-foreground">Classic Outlook for Windows: <strong>Add Calendar</strong>, <strong>From Internet</strong>, paste the address.</p>
        </div>
        <div>
          <h4 className="font-semibold">Google Calendar</h4>
          <ol className="ml-5 list-decimal">
            <li>Beside <strong>Other calendars</strong>, choose <strong>+</strong>, then <strong>From URL</strong>.</li>
            <li>Paste the address and choose <strong>Add calendar</strong>.</li>
          </ol>
          <p className="mt-1 text-muted-foreground">Google can take up to a day to fetch changes.</p>
        </div>
        <div>
          <h4 className="font-semibold">Apple Calendar</h4>
          <ol className="ml-5 list-decimal">
            <li>On a Mac: <strong>File</strong>, <strong>New Calendar Subscription</strong>, paste the address, <strong>Subscribe</strong>.</li>
            <li>On iPhone or iPad: <strong>Settings</strong>, <strong>Calendar</strong>, <strong>Accounts</strong>, <strong>Add Account</strong>, <strong>Other</strong>, <strong>Add Subscribed Calendar</strong>.</li>
          </ol>
        </div>
      </div>
    </section>
  );
}

// ---- licence utilisation ----------------------------------------------------------------------------------------------

function LicencesTab() {
  const q = useApiQuery<{ rows: PoolTotalsRow[]; total: Utilisation & { pools: number; overused_rows: number } }>("license-pools/totals");
  const r = q.data;
  return (
    <>
      <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
        <p className="max-w-prose text-sm text-muted-foreground">Seats per application and SKU, over every license pool. Assigned may exceed purchased: overuse is listed, not refused.</p>
        <ExportButton path="reports/license-utilization/export.csv" fallback="softwarden-licence-utilisation.csv" what="license utilization" />
      </div>
      {q.isError && <ErrorBanner message={`${problemText(q.error, "Couldn't load license utilization.")} Nothing was changed.`} onRetry={() => void q.refetch()} />}
      {q.isPending && <Loading what="license utilization" rows={3} />}
      {r && r.rows.length === 0 && (
        <EmptyState title="No license pools yet" action={<Button asChild variant="outline"><Link to="/license-pools">Open License pools</Link></Button>}>
          A license pool records the seats of one SKU an application uses.
        </EmptyState>
      )}
      {r && r.rows.length > 0 && (
        <TableRegion label="License utilization table">
          <Table>
            <TableCaption className="sr-only">Seats per application and SKU, with the organization's total last</TableCaption>
            <TableHeader><TableRow>
              <TableHead scope="col" aria-sort="ascending">Application</TableHead><TableHead scope="col">SKU</TableHead>
              <TableHead scope="col" className="text-right">Pools</TableHead><TableHead scope="col">Utilization</TableHead>
            </TableRow></TableHeader>
            <TableBody>
              {r.rows.map((x) => (
                <TableRow key={`${x.application.id}-${x.sku_id || x.sku_name}`}>
                  <TableHead scope="row" className="whitespace-normal font-normal text-foreground">
                    <Link to={`/license-pools?application=${x.application.id}`} className="inline-block min-h-6 leading-6">{x.application.name}</Link>
                    {x.application.archived && <span className="block text-sm text-muted-foreground">Archived</span>}
                  </TableHead>
                  <TableCell className="whitespace-normal">{x.sku_name}{x.sku_id && <span className="block font-mono text-xs text-muted-foreground">{x.sku_id}</span>}</TableCell>
                  <TableCell className="tabular text-right">{x.pools}</TableCell>
                  <TableCell>
                    <UtilisationMeter purchased={x.seats_purchased} assigned={x.seats_assigned} utilization={x.utilization} />
                    {x.overused && <span className="mt-1 block"><OverusedChip by={-x.seats_available} /></span>}
                  </TableCell>
                </TableRow>
              ))}
              <TableRow className="font-semibold">
                <TableHead scope="row" className="text-foreground">All applications</TableHead>
                <TableCell />
                <TableCell className="tabular text-right">{r.total.pools}</TableCell>
                <TableCell><UtilisationMeter purchased={r.total.seats_purchased} assigned={r.total.seats_assigned} utilization={r.total.utilization} /></TableCell>
              </TableRow>
            </TableBody>
          </Table>
        </TableRegion>
      )}
    </>
  );
}
