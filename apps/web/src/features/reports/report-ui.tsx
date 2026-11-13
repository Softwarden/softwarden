/** Pieces the dashboard and the Reports page share: a deadline's date and words, the deadlines table,
 *  a spend group's name (a named gap is the gap chip, "Everything else" a neutral chip with how many it folds), the
 *  per-currency totals and the spend groups table. No charts here, so the Reports page loads without Recharts. */
import { Link } from "react-router-dom";
import { chip, GapChip, StatTile } from "@/components/sw";
import { TableRegion } from "@/components/app";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { moneyText } from "@/features/money/contract-types";
import { cn } from "@/lib/utils";
import type { Commitment, DeadlineRow, SpendGroup } from "./report-types";

export const money = (amount: string | null | undefined, currency: string | null | undefined) => moneyText(amount, currency ?? "") ?? "—";

/** "12,000.00 USD" in tabular figures, or a dash. */
export function Amount({ amount, currency, className }: { amount: string | null | undefined; currency: string | null | undefined; className?: string }) {
  const text = moneyText(amount, currency ?? "");
  return text ? <span className={cn("tabular whitespace-nowrap", className)}>{text}</span> : <span className="text-muted-foreground">—</span>;
}

/** The deadline's date, how it was worked out and how far off it is. A row whose stored date its computation doesn't
 *  explain has `label` null and no explanation: its `label_text` ("Cancellation deadline") is shown alone. */
export function DeadlineWhen({ row, explain = true }: { row: DeadlineRow; explain?: boolean }) {
  const t = row.timing;
  return (
    <span className="inline-flex flex-col gap-0.5">
      <span className="inline-flex flex-wrap items-center gap-1.5">
        <span className="tabular font-semibold">{row.date}</span>
        {row.label ? (
          <span className={chip({ state: row.label === "assumed" ? "gap" : "neutral" })} data-label={row.label}>
            {row.label === "assumed" && <span aria-hidden="true">⚠</span>}{row.label_text}
          </span>
        ) : row.label_text && <span className="text-sm text-muted-foreground" data-label="">{row.label_text}</span>}
      </span>
      <span className={t.overdue ? "text-sm font-semibold text-error-foreground" : "text-sm text-muted-foreground"}>{t.overdue && <span aria-hidden="true">! </span>}{t.text}</span>
      {explain && row.label && row.explanation && <span className="block max-w-xs whitespace-normal text-xs text-muted-foreground" data-testid="deadline-explanation">{row.explanation}</span>}
    </span>
  );
}

const ACTION_WORDS: Record<DeadlineRow["action"], string> = { cancel_by: "Cancel by", renew_by: "Renew by" };

/** One row per open action deadline: renewals (their open cycle, with the contract that drives it) and contracts that
 *  drive no renewal. The name links to the renewal's or the contract's page. */
export function DeadlinesTable({ rows, caption, label, cost = true, owner = true }: {
  rows: DeadlineRow[]; caption: string; label: string; cost?: boolean; owner?: boolean;
}) {
  // Below 640 px each deadline is a stacked row with its labels (queue-table), not a table cut off at the
  // side.
  return (
    <TableRegion label={label} className="rounded-none border-0">
      <Table className="queue-table">
        <TableCaption className="sr-only">{caption}</TableCaption>
        <TableHeader><TableRow>
          <TableHead scope="col" aria-sort="ascending">Deadline</TableHead>
          <TableHead scope="col">Renewal or contract</TableHead>
          <TableHead scope="col">Action</TableHead>
          <TableHead scope="col">Vendor</TableHead>
          {cost && <TableHead scope="col" className="text-right">Annualized cost</TableHead>}
          {owner && <TableHead scope="col">IT follow-up</TableHead>}
        </TableRow></TableHeader>
        <TableBody>
          {rows.map((r) => (
            <TableRow key={r.uid} className="align-top" data-uid={r.uid}>
              <TableCell data-label="Deadline"><DeadlineWhen row={r} /></TableCell>
              <TableHead scope="row" className="whitespace-normal font-normal text-foreground">
                <Link to={r.path} className="inline-block min-h-6 font-semibold leading-6">{r.name}</Link>
                <span className="block text-sm text-muted-foreground">
                  {r.source === "renewal" ? "Renewal" : "Contract"}
                  {r.source === "renewal" && r.contract && <>, dates from <Link to={`/contracts/${r.contract.id}`} className="underline">{r.contract.name}</Link></>}
                  {r.decision && <>, decision recorded: {r.decision}</>}
                </span>
              </TableHead>
              <TableCell data-label="Action">
                {ACTION_WORDS[r.action]}
                {r.term_ends
                  ? <span className="block text-sm text-muted-foreground">{r.source === "contract" ? "Term ends" : "Renews"} {r.term_ends}</span>
                  : <span className="mt-1 block"><GapChip label={r.source === "contract" ? "No end date" : "No renewal date"} /></span>}
              </TableCell>
              <TableCell data-label="Vendor">{r.vendor ?? <GapChip label="No vendor" />}</TableCell>
              {cost && <TableCell data-label="Annualized cost" className="sm:text-right"><Amount amount={r.annualized_cost} currency={r.currency} /></TableCell>}
              {owner && <TableCell data-label="IT follow-up">{r.owner?.name ?? <span className="text-muted-foreground">—</span>}</TableCell>}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableRegion>
  );
}

/** A spend group's name: the group, a named gap (never folded) or "Everything else". */
export function GroupName({ g }: { g: Pick<SpendGroup, "label" | "gap" | "groups"> }) {
  if (g.gap === "other") return <span className={chip({ state: "neutral" })} data-gap="other"><span aria-hidden="true">…</span>{g.label}{g.groups ? ` (${g.groups} more)` : ""}</span>;
  if (g.gap) return <GapChip label={g.label} />;
  return <>{g.label}</>;
}

/** Per currency: the annualised total (what every dimension adds up to), run rate, one-time charges and Signed terms
 *  not started yet. Never summed across currencies. */
export function CommitmentTiles({ c, idPrefix }: { c: Commitment; idPrefix: string }) {
  const id = `${idPrefix}-${c.currency.toLowerCase()}`;
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <StatTile id={`${id}-annualized`} label={`Annualized spend, ${c.currency}`} value={money(c.annualized, c.currency)}
        detail={`${c.contracts} live ${c.contracts === 1 ? "contract" : "contracts"} (Active, Pending renewal)`} />
      <StatTile id={`${id}-run-rate`} label="Run rate" value={money(c.run_rate, c.currency)} detail="Recurring cost a year" />
      <StatTile id={`${id}-one-time`} label="One-time charges" value={money(c.one_time, c.currency)} detail="On live terms" />
      <StatTile id={`${id}-future`} label="Signed, not started" value={money(c.future, c.currency)}
        detail={c.next_start ? `Not spend yet; the first starts ${c.next_start}` : "No Signed terms waiting to start"} />
    </div>
  );
}

/** The groups of one dimension in one currency: name (or gap), amount, contracts and records. */
export function SpendGroupsTable({ rows, currency, by, label }: { rows: SpendGroup[]; currency: string; by: string; label: string }) {
  return (
    <TableRegion label={label} className="rounded-none border-0">
      <Table>
        <TableCaption className="sr-only">Annualized spend in {currency} by {by}, largest first; gaps are named, not folded</TableCaption>
        <TableHeader><TableRow>
          <TableHead scope="col">{by.charAt(0).toUpperCase() + by.slice(1)}</TableHead>
          <TableHead scope="col" aria-sort="descending" className="text-right">Annualized</TableHead>
          <TableHead scope="col" className="text-right">Contracts</TableHead>
          <TableHead scope="col" className="text-right">Records</TableHead>
        </TableRow></TableHeader>
        <TableBody>
          {rows.map((g, i) => (
            <TableRow key={`${g.gap ?? "group"}-${g.key ?? i}`} data-gap={g.gap ?? ""}>
              <TableHead scope="row" className="whitespace-normal font-normal text-foreground"><GroupName g={g} /></TableHead>
              <TableCell className="text-right"><Amount amount={g.amount} currency={g.currency} /></TableCell>
              <TableCell className="tabular text-right">{g.contracts ?? "—"}</TableCell>
              <TableCell className="tabular text-right">{g.records ?? "—"}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableRegion>
  );
}

/** Shown in place of a dashboard block or report the organisation's edition doesn't include: unavailable, never zero. */
export function Unavailable({ what, why }: { what: string; why: string }) {
  return (
    <p className="rounded-md border border-dashed border-border-strong bg-muted px-3 py-2 text-sm" data-unavailable="">
      <span aria-hidden="true">⊘ </span><span className="font-semibold">{what} isn't available.</span> {why}
    </p>
  );
}

/** A titled, bordered block with an optional link or action at the right of its heading. */
export function Block({ id, title, action, children, className, testId }: {
  id: string; title: string; action?: React.ReactNode; children: React.ReactNode; className?: string; testId?: string;
}) {
  return (
    <section aria-labelledby={id} className={cn("overflow-hidden rounded-lg border bg-card", className)} data-testid={testId}>
      <div className="flex min-h-[49px] flex-wrap items-center gap-x-3 gap-y-1 border-b px-4 py-2.5">
        <h2 id={id} tabIndex={-1} className="font-display text-section font-semibold outline-none">{title}</h2>
        {action && <div className="ml-auto flex flex-wrap items-center gap-2 text-sm">{action}</div>}
      </div>
      <div>{children}</div>
    </section>
  );
}
