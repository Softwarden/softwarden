/** End-of-life words and chips (capability `eol`), shared by the calendar (`/end-of-life`), a record's
 *  End of life panel and the Needs attention "End of life" queue. The status is derived by the API (eol/status.py) and
 *  shown as a glyph plus words, never colour alone; a version the API can't place is a named gap, not "Unknown". Kept
 *  small: Needs attention is in the first bundle. */
import { chip, GapChip } from "@/components/sw";
import type { Schemas } from "@/lib/openapi";

export type TrackedVersion = Schemas["TrackedVersion"];
export type EolStatus = Schemas["EolStatusEnum"];

/** How far ahead "Ends in N days" takes the pending tone: the Needs attention horizon (eol/status.NEAR_DAYS). */
export const NEAR_DAYS = 180;

const days = (n: number) => `${n} ${n === 1 ? "day" : "days"}`;

/** "Ends in 42 days", "Ends today", "Ended 3 days ago"; "" with no date. */
export function endsText(daysRemaining: number | null | undefined): string {
  if (daysRemaining === null || daysRemaining === undefined) return "";
  if (daysRemaining === 0) return "Ends today";
  return daysRemaining > 0 ? `Ends in ${days(daysRemaining)}` : `Ended ${days(-daysRemaining)} ago`;
}

type Row = Pick<TrackedVersion, "status" | "days_remaining" | "unknown_reason">;

/** The record's own reasons for an unknown status, as named gaps (eol/reads.RECORD_REASONS). */
const RECORD_GAPS: Partial<Record<NonNullable<TrackedVersion["unknown_reason"]>, string>> = {
  cycle_not_listed: "Release cycle not listed",
  no_version_match: "Version matches no release cycle",
  no_version: "No version recorded",
};

/** Why a version is unknown, as a named gap, or null when it isn't the record's gap (the installation's data is
 *  missing: not downloaded yet, a failed download, downloads off; said with a neutral chip instead). */
export function unknownGap(r: Row): string | null {
  if (r.status !== "unknown" || !r.unknown_reason) return null;
  return RECORD_GAPS[r.unknown_reason] ?? null;
}

/** The status as one chip: ! Past end of life, ◐ Ends in 42 days (within 180 days), ◑ Security fixes only, ✓ Supported,
 *  or for an unknown version its named gap when the record is why, else the installation's state (○ Waiting for
 *  endoflife.date data, ⊘ endoflife.date data couldn't be read, ⊘ endoflife.date downloads are off). */
export function EolStatusChip({ row }: { row: Row }) {
  const d = row.days_remaining;
  if (row.status === "end_of_life") {
    return <span className={chip({ state: "overdue" })} data-eol-status="end_of_life"><span aria-hidden="true">!</span>{d === 0 ? "Ends today" : "Past end of life"}</span>;
  }
  if (row.status === "unknown") {
    const gap = unknownGap(row);
    if (gap) return <span data-eol-status="unknown"><GapChip label={gap} /></span>;
    if (row.unknown_reason === "download_failed") {
      return <span className={chip({ state: "unreviewed" })} data-eol-status="unknown"><span aria-hidden="true">⊘</span>endoflife.date data couldn't be read</span>;
    }
    if (row.unknown_reason === "downloads_off") {
      return <span className={chip({ state: "unreviewed" })} data-eol-status="unknown"><span aria-hidden="true">⊘</span>endoflife.date downloads are off</span>;
    }
    return <span className={chip({ state: "unreviewed" })} data-eol-status="unknown"><span aria-hidden="true">○</span>Waiting for endoflife.date data</span>;
  }
  if (d !== null && d !== undefined && d <= NEAR_DAYS) {
    return <span className={chip({ state: "pending" })} data-eol-status={row.status}><span aria-hidden="true">◐</span>{endsText(d)}</span>;
  }
  if (row.status === "security_only") {
    return <span className={chip({ state: "pending" })} data-eol-status="security_only"><span aria-hidden="true">◑</span>Security fixes only</span>;
  }
  return <span className={chip({ state: "confirmed" })} data-eol-status="supported"><span aria-hidden="true">✓</span>Supported</span>;
}

/** The line under the chip: the other half of the status in words ("Security fixes only", "Ends in 300 days"). */
export function eolDetail(row: Pick<TrackedVersion, "status" | "days_remaining">): string {
  const d = row.days_remaining;
  const near = d !== null && d !== undefined && d <= NEAR_DAYS;
  if (row.status === "end_of_life") return d ? endsText(d) : "";
  if (row.status === "security_only") return near ? "Security fixes only" : endsText(d);
  if (row.status === "supported") return near ? "" : endsText(d);
  return "";
}
