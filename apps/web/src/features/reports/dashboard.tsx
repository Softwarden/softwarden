/** Dashboard (everyone's home since 2026-11-05, superseding the plan's "home for Admins"):
 *  `/dashboard`, loaded on first use with Recharts. One request, `GET /dashboard`, gives every block:
 *
 *  - Needs attention: the badge counts per scope, each linking to Needs attention in that scope (no
 *    per-queue table here; Needs attention lists every queue).
 *  - Deadlines: overdue and due within 30, 60 and 90 days (toggles in the URL, `due`), the 10 soonest with links to the
 *    renewal or contract, and the Renewals calendar for the rest.
 *  - Expiring credentials: API tokens expiring within 30 days or expired in the last 30 (an Admin's: the
 *    organisation's; anyone else's: their own).
 *  - Spend: per currency (never summed across currencies; `currency` in the URL), by vendor, department or category
 *    (tabs in the URL, `spend`): the largest 8 groups, every named gap and "Everything else", as a bar chart with the
 *    same rows in a table under it.
 *  - Licence utilisation and the data-quality summary (the panel).
 *
 *  A block the edition lacks comes back null and is shown as unavailable, never as zero. Every role reads the same
 *  dashboard; nothing here writes. */
import { useMemo } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { AllClear, EmptyState, ErrorBanner, Loading, PageHead, TabStrip, TableRegion } from "@/components/app";
import { ChartCard, chip, StatTile } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { OverusedChip, UtilisationMeter } from "@/features/money/contract-ui";
import { DataQualitySection } from "@/features/quality/dashboard";
import { problemText, useApiQuery } from "@/lib/query";
import { useMe } from "@/lib/session";
import { cn } from "@/lib/utils";
import type { Credential, Dashboard, DeadlineRow, SpendGroup } from "./report-types";
import { Block, CommitmentTiles, DeadlinesTable, SpendGroupsTable, Unavailable } from "./report-ui";
import { SPEND_LEGEND, SpendChart } from "./spend-chart";

export function DashboardPage() {
  const me = useMe();
  const q = useApiQuery<Dashboard>("dashboard");
  const d = q.data;
  return (
    <>
      <PageHead title="Dashboard" desc={d ? `Where ${me.organization.name} stands today, ${d.as_of}: what needs attention, what is due, what it spends and how complete the catalog is.` : `Where ${me.organization.name} stands today.`}>
        <Button asChild variant="outline"><Link to="/reports">Reports</Link></Button>
      </PageHead>
      {q.isError && <ErrorBanner message={`${problemText(q.error, "Couldn't load the dashboard.")} Nothing was changed.`} onRetry={() => void q.refetch()} />}
      {q.isPending && <Loading what="the dashboard" rows={5} />}
      {d && (
        <div className="grid gap-6">
          <AttentionBlock d={d} />
          <DeadlinesBlock d={d} />
          <SpendBlock d={d} />
          <div className="grid items-start gap-6 xl:grid-cols-2">
            <LicencesBlock d={d} />
            <CredentialsBlock d={d} />
          </div>
          {d.data_quality
            ? <DataQualitySection s={d.data_quality} />
            : (
              <section aria-labelledby="h-dq">
                <h2 id="h-dq" className="mb-3 font-display text-section font-semibold">Data quality</h2>
                <Unavailable what="Data quality" why="This organization's edition doesn't include data-quality checks." />
              </section>
            )}
        </div>
      )}
    </>
  );
}

// ---- Needs attention --------------------------------------------------------------------------------------------------

const SCOPES = [["mine", "Assigned to me"], ["all", "All"], ["unassigned", "Unassigned"]] as const;
const scopePath = (scope: string) => (scope === "mine" ? "/" : `/?scope=${scope}`);

/** The three scopes' counts, each opening Needs attention in that scope, where every queue has its jump link (a
 *  per-queue table here repeated that page and was removed). */
function AttentionBlock({ d }: { d: Dashboard }) {
  const { counts } = d.needs_attention;
  return (
    <Block id="h-attention" title="Needs attention" action={<Link to="/">Open Needs attention</Link>} testId="dash-attention">
      <div className="grid gap-3 p-4 sm:grid-cols-3">
        {SCOPES.map(([scope, label]) => (
          <StatTile key={scope} id={`dash-attention-${scope}`} label={label}
            value={<Link to={scopePath(scope)} className="text-inherit no-underline hover:underline">{counts[scope]}<span className="sr-only"> {counts[scope] === 1 ? "item" : "items"} in {label}, open Needs attention</span></Link>}
            detail={scope === "mine" ? "The navigation badge counts these." : scope === "all" ? "Every item in the organization." : "No IT steward or owner to route to."} />
        ))}
      </div>
    </Block>
  );
}

// ---- deadlines --------------------------------------------------------------------------------------------------------

type Due = "all" | "overdue" | "30" | "60" | "90";
const inWindow = (r: DeadlineRow, due: Due) => due === "all" || (due === "overdue" ? r.timing.overdue : !r.timing.overdue && r.timing.days <= Number(due));

function DeadlinesBlock({ d }: { d: Dashboard }) {
  const [params, setParams] = useSearchParams();
  const raw = params.get("due");
  const due: Due = raw === "overdue" || raw === "30" || raw === "60" || raw === "90" ? raw : "all";
  const setDue = (v: Due) => setParams((cur) => { const next = new URLSearchParams(cur); if (v === "all") next.delete("due"); else next.set("due", v); return next; });
  const link = <Link to="/reports?tab=calendar">Renewals calendar</Link>;
  if (!d.deadlines) {
    return (
      <Block id="h-deadlines" title="Deadlines" testId="dash-deadlines">
        <div className="p-4"><Unavailable what="Deadlines" why="This organization's edition includes neither renewals nor contracts." /></div>
      </Block>
    );
  }
  const dl = d.deadlines;
  const rows = dl.rows.filter((r) => inWindow(r, due));
  const toggles: { id: Due; label: string; count: number }[] = [
    { id: "all", label: "All, next 90 days", count: dl.total },
    { id: "overdue", label: "Overdue", count: dl.overdue },
    ...dl.windows.map((w) => ({ id: String(w.days) as Due, label: `Within ${w.days} days`, count: w.count })),
  ];
  return (
    <Block id="h-deadlines" title="Deadlines" action={link} testId="dash-deadlines">
      <div className="flex flex-wrap items-center gap-2 px-4 pt-3" role="group" aria-label="Show deadlines">
        {toggles.map((t) => (
          <button key={t.id} type="button" aria-pressed={due === t.id} onClick={() => setDue(t.id)} data-due={t.id}
            className={cn("inline-flex min-h-8 items-center gap-2 rounded-md border border-border-strong bg-card px-3 text-[13px] text-muted-foreground aria-pressed:border-accent-line aria-pressed:bg-accent aria-pressed:font-semibold aria-pressed:text-accent-foreground",
              t.id === "overdue" && t.count > 0 && "text-error-foreground")}>
            {t.id === "overdue" && t.count > 0 && <span aria-hidden="true">!</span>}{t.label}
            <span className="tabular rounded-sm bg-muted px-1.5 text-xs font-semibold text-foreground">{t.count}</span>
          </button>
        ))}
      </div>
      <p className="px-4 pt-2 text-sm text-muted-foreground" role="status">{deadlineNote(dl.total, rows.length, toggles.find((t) => t.id === due)!.count, due)}</p>
      <div className="pt-2">
        {dl.total === 0
          ? <div className="px-4 pb-4"><AllClear>No action deadlines in the next 90 days, and none overdue.</AllClear></div>
          : rows.length === 0
            ? <p className="px-4 pb-4 text-sm text-muted-foreground">None of the 10 soonest is in this window. The {link} lists every deadline.</p>
            : <DeadlinesTable rows={rows} label="Deadlines table" caption="Action deadlines, soonest first: the renewal or contract, what to do by when, and its cost" owner={false} />}
      </div>
    </Block>
  );
}

/** What the list shows: the dashboard has the 10 soonest; the Renewals calendar has every deadline. */
function deadlineNote(total: number, shown: number, count: number, due: Due) {
  if (total === 0) return "";
  const noun = (n: number) => `${n} ${n === 1 ? "deadline" : "deadlines"}`;
  if (due === "all") return `The ${Math.min(10, total)} soonest of ${noun(total)} up to 90 days ahead, overdue included.`;
  if (count > shown) return `Showing ${shown} of ${noun(count)}: the dashboard lists the 10 soonest; the Renewals calendar lists every one.`;
  return `${noun(count)}.`;
}

// ---- spend ------------------------------------------------------------------------------------------------------------

type SpendDim = "vendor" | "department" | "category";
const DIMS: { id: SpendDim; label: string }[] = [{ id: "vendor", label: "By vendor" }, { id: "department", label: "By department" }, { id: "category", label: "By category" }];

function SpendBlock({ d }: { d: Dashboard }) {
  const [params, setParams] = useSearchParams();
  const spend = d.spend;
  const currencies = useMemo(() => spend?.totals.map((t) => t.currency) ?? [], [spend]);
  const asked = params.get("currency") ?? "";
  const currency = currencies.includes(asked) ? asked : currencies[0] ?? "";
  const rawDim = params.get("spend");
  const dim: SpendDim = rawDim === "department" || rawDim === "category" ? rawDim : "vendor";
  const set = (k: string, v: string, dflt: string) => setParams((cur) => { const next = new URLSearchParams(cur); if (v === dflt) next.delete(k); else next.set(k, v); return next; });
  const detail = <Link to="/reports?tab=spend">Spend report</Link>;
  if (!spend) {
    return (
      <Block id="h-spend" title="Spend" testId="dash-spend">
        <div className="p-4"><Unavailable what="Spend" why="This organization's edition doesn't include contracts." /></div>
      </Block>
    );
  }
  if (!currencies.length) {
    return (
      <Block id="h-spend" title="Spend" action={detail} testId="dash-spend">
        <div className="p-4">
          <EmptyState title="No spend yet" action={<Button asChild variant="outline"><Link to="/contracts">Open Contracts</Link></Button>}>
            Spend is the annualized cost of Active and Pending renewal contracts. Add a contract with an amount, or import contracts, to see it here.
          </EmptyState>
        </div>
      </Block>
    );
  }
  const total = spend.totals.find((t) => t.currency === currency)!;
  const rows: SpendGroup[] = spend[`by_${dim}`].filter((g) => g.currency === currency);
  const dimLabel = DIMS.find((x) => x.id === dim)!.label.replace("By ", "");
  const gaps = rows.filter((g) => g.gap && g.gap !== "other").length;
  return (
    <Block id="h-spend" title="Spend" action={detail} testId="dash-spend">
      <div className="grid gap-4 p-4">
        {currencies.length > 1 && (
          <div className="flex flex-wrap items-center gap-2">
            <span id="spend-currency-label" className="text-sm font-medium">Currency</span>
            <div className="inline-flex overflow-hidden rounded-md border border-border-strong bg-card" role="group" aria-labelledby="spend-currency-label">
              {currencies.map((c) => (
                <button key={c} type="button" aria-pressed={c === currency} onClick={() => set("currency", c, currencies[0])}
                  className="flex h-8 items-center px-3 text-[13px] text-muted-foreground aria-pressed:bg-accent aria-pressed:font-semibold aria-pressed:text-accent-foreground [&+&]:border-l [&+&]:border-border-strong">
                  {c}
                </button>
              ))}
            </div>
            <span className="text-sm text-muted-foreground">Amounts are never converted or added across currencies.</span>
          </div>
        )}
        <CommitmentTiles c={total} idPrefix="dash-spend" />
        <div>
          <TabStrip idPrefix="spend-" label="Spend grouped by" tabs={DIMS} tab={dim} onSelect={(t) => set("spend", t, "vendor")} />
          <div role="tabpanel" id="spend-tabpanel" aria-labelledby={`spend-tab-${dim}`} className="grid gap-4">
            <ChartCard id="chart-spend" title={`Annualized spend in ${currency}, by ${dimLabel.toLowerCase()}`} legend={SPEND_LEGEND}
              summary={`${rows.length} ${rows.length === 1 ? "row" : "rows"}: the largest groups, ${gaps ? `${gaps} named ${gaps === 1 ? "gap" : "gaps"}` : "no gaps"}${rows.some((g) => g.gap === "other") ? " and everything else together" : ""}. The table below has the same numbers.`}>
              {rows.length ? <SpendChart rows={rows} currency={currency} testId="spend-chart" /> : <p className="px-2 text-sm text-muted-foreground">Nothing in {currency}.</p>}
            </ChartCard>
            {rows.length > 0 && (
              <div className="overflow-hidden rounded-lg border bg-card">
                <SpendGroupsTable rows={rows} currency={currency} by={dimLabel.toLowerCase()} label={`Spend by ${dimLabel.toLowerCase()} table`} />
              </div>
            )}
          </div>
        </div>
      </div>
    </Block>
  );
}

// ---- licence utilisation ----------------------------------------------------------------------------------------------

function LicencesBlock({ d }: { d: Dashboard }) {
  const lu = d.license_utilization;
  const link = <Link to="/license-pools">License pools</Link>;
  if (!lu) {
    return (
      <Block id="h-licences" title="License utilization" testId="dash-licences">
        <div className="p-4"><Unavailable what="License utilization" why="This organization's edition doesn't include contracts and license pools." /></div>
      </Block>
    );
  }
  const t = lu.total;
  return (
    <Block id="h-licences" title="License utilization" action={link} testId="dash-licences">
      {t.pools === 0 ? (
        <div className="p-4"><EmptyState title="No license pools yet">A license pool records the seats of one SKU an application uses. Add one from License pools, or import them.</EmptyState></div>
      ) : (
        <>
          <div className="grid gap-3 p-4 sm:grid-cols-3">
            <StatTile id="dash-seats-assigned" label="Seats assigned" value={`${t.seats_assigned} of ${t.seats_purchased}`} detail={`In ${t.pools} ${t.pools === 1 ? "pool" : "pools"}`} />
            <StatTile id="dash-seats-utilisation" label="Utilization" value={t.utilization != null ? `${t.utilization}%` : "—"} detail={t.utilization == null ? "No seats purchased" : "Assigned of purchased"} />
            <StatTile id="dash-seats-overused" label="Overused" value={t.overused_rows}
              detail={t.overused_rows ? <><span aria-hidden="true">! </span>{t.overused_rows === 1 ? "application and SKU" : "applications and SKUs"} over what was bought</> : "Nothing assigned beyond what was bought"} />
          </div>
          <TableRegion label="License utilization table" className="rounded-none border-0 border-t">
            <Table>
              <TableCaption className="sr-only">License utilization per application and SKU: overused first, then the most used; at most 8</TableCaption>
              <TableHeader><TableRow>
                <TableHead scope="col">Application and SKU</TableHead><TableHead scope="col" aria-sort="descending">Utilization</TableHead>
              </TableRow></TableHeader>
              <TableBody>
                {lu.rows.map((r) => (
                  <TableRow key={`${r.application.id}-${r.sku_id || r.sku_name}`}>
                    <TableHead scope="row" className="whitespace-normal font-normal text-foreground">
                      <Link to={`/license-pools?application=${r.application.id}`} className="inline-block min-h-6 font-semibold leading-6">{r.application.name}</Link>
                      <span className="block text-sm text-muted-foreground">{r.sku_name}{r.pools > 1 ? `, ${r.pools} pools` : ""}</span>
                    </TableHead>
                    <TableCell>
                      <UtilisationMeter purchased={r.seats_purchased} assigned={r.seats_assigned} utilization={r.utilization} />
                      {r.overused && <span className="mt-1 block"><OverusedChip by={-r.seats_available} /></span>}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableRegion>
        </>
      )}
    </Block>
  );
}

// ---- expiring credentials ---------------------------------------------------------------------------------------------

function CredentialStatus({ c }: { c: Credential }) {
  if (c.expired) return <span className={chip({ state: "overdue" })}><span aria-hidden="true">!</span>Expired {-c.days === 0 ? "today" : `${-c.days} ${-c.days === 1 ? "day" : "days"} ago`}</span>;
  return <span className={chip({ state: "pending" })}><span aria-hidden="true">◐</span>{c.days === 0 ? "Expires today" : `Expires in ${c.days} ${c.days === 1 ? "day" : "days"}`}</span>;
}

function CredentialsBlock({ d }: { d: Dashboard }) {
  const me = useMe();
  const ec = d.expiring_credentials;
  const admin = me.role === "admin";
  return (
    <Block id="h-credentials" title="Expiring credentials" action={admin ? <Link to="/admin/tokens">API tokens</Link> : undefined} testId="dash-credentials">
      <p className="px-4 pt-3 text-sm text-muted-foreground">
        {admin ? "The organization's API tokens" : "Your API tokens"} that expire within {ec.days} days or expired in the last {ec.days}. Discovered app secrets and certificates join this list with discovery.
      </p>
      {ec.rows.length === 0
        ? <div className="p-4"><AllClear>No API tokens expire in the next {ec.days} days.</AllClear></div>
        : (
          <TableRegion label="Expiring credentials table" className="mt-2 rounded-none border-0 border-t">
            <Table>
              <TableCaption className="sr-only">API tokens expiring soon or recently expired, soonest first</TableCaption>
              <TableHeader><TableRow>
                <TableHead scope="col">Token</TableHead><TableHead scope="col">Owner</TableHead>
                <TableHead scope="col" aria-sort="ascending">Expires</TableHead><TableHead scope="col">Status</TableHead>
              </TableRow></TableHeader>
              <TableBody>
                {ec.rows.map((c) => (
                  <TableRow key={c.id} data-credential={c.id}>
                    <TableHead scope="row" className="whitespace-normal font-semibold text-foreground">{c.name}<span className="block font-mono text-xs font-normal text-muted-foreground">{c.prefix}…</span></TableHead>
                    <TableCell>{c.owner?.name ?? <span className="text-muted-foreground">—</span>}</TableCell>
                    <TableCell className="tabular">{c.expires_on}</TableCell>
                    <TableCell><CredentialStatus c={c} /></TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableRegion>
        )}
    </Block>
  );
}
