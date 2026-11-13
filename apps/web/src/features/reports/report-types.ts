/** The dashboard, reports and calendar feed shapes: `GET /dashboard`, `/reports/spend`,
 *  `/reports/renewals-calendar` and `/calendar-feed` as money/report_serializers.py describes them. Money is a decimal
 *  string next to its currency, never summed across currencies and never turned into a float except to size a bar. */
import type { AttentionQueue, QualitySummary, Timing, UserRef } from "@/api";

export type Ref = { id: string; name: string };

/** Per currency: what every spend dimension adds up to (`annualized` = `run_rate` + `one_time`), and Signed terms not
 *  started yet (`future`), which are not spend. */
export type Commitment = {
  currency: string; annualized: string; run_rate: string; one_time: string; future: string; future_one_time: string;
  next_start: string | null; contracts: number;
};

/** Why cost has no group, or `other` for the dashboard's "Everything else" row (never a named gap folded in). */
export type SpendGap = "no_vendor" | "unallocated" | "no_department" | "no_category" | "not_application" | "no_cost_center" | "other";
export type SpendGroup = {
  key: string | null; label: string; gap: SpendGap | null; currency: string; amount: string; contracts: number | null; records: number | null;
  /** With gap `other`: how many groups it folds (dashboard only). */
  groups?: number;
};
export type AllocationRow = {
  key: string | null; label: string; gap: SpendGap | null; currency: string; amount: string; percent: string;
  contract: Ref; vendor: Ref | null; record: (Ref & { kind: string }) | null; cost_center: string;
};
export type SpendBy = "vendor" | "department" | "category" | "cost_center";
export const SPEND_BY: { id: SpendBy; label: string; noun: string }[] = [
  { id: "vendor", label: "Vendor", noun: "vendor" },
  { id: "department", label: "Department", noun: "department" },
  { id: "category", label: "Category", noun: "category" },
  { id: "cost_center", label: "Cost center", noun: "cost center" },
];
export type SpendReport = { by: SpendBy; totals: Commitment[]; rows: SpendGroup[]; allocations: AllocationRow[] };

export type DeadlineRow = {
  uid: string; source: "renewal" | "contract"; id: string; name: string; vendor: string | null; date: string;
  action: "cancel_by" | "renew_by"; label: string | null; label_text: string | null; explanation: string;
  term_ends: string | null; auto_renew: string; decision: string | null; timing: Timing; contract: Ref | null;
  annualized_cost: string | null; currency: string | null; owner: UserRef | null; path: string;
};
export type CalendarReport = {
  months: number; from: string; to: string; overdue: number; by_month: { month: string; count: number }[]; rows: DeadlineRow[];
};

export type Credential = {
  kind: "api_token" | string; id: string; name: string; prefix: string; owner: UserRef | null; expires_on: string; days: number; expired: boolean;
};

export type Utilisation = { seats_purchased: number; seats_assigned: number; seats_available: number; utilization: number | null; overused: boolean };
export type PoolTotalsRow = Utilisation & { application: Ref & { archived: boolean }; sku_name: string; sku_id: string; pools: number };

export type Dashboard = {
  as_of: string;
  needs_attention: {
    counts: { mine: number; all: number; unassigned: number };
    queues: { queue: AttentionQueue | string; all: number; mine: number; unassigned: number; counted: boolean }[];
  };
  /** Null without renewals and contracts. */
  deadlines: { overdue: number; windows: { days: number; count: number }[]; total: number; rows: DeadlineRow[] } | null;
  expiring_credentials: { days: number; count: number; rows: Credential[] };
  /** Null without the contracts capability. */
  spend: { totals: Commitment[]; by_vendor: SpendGroup[]; by_department: SpendGroup[]; by_category: SpendGroup[] } | null;
  license_utilization: { total: Utilisation & { pools: number; overused_rows: number }; rows: PoolTotalsRow[] } | null;
  /** Null without data_quality. */
  data_quality: QualitySummary | null;
};

export type CalendarFeed = { active: boolean; id: string | null; prefix: string | null; created_at: string | null; last_used_at: string | null };
export type CalendarFeedCreated = CalendarFeed & { url: string };

/** "2026-11" as "November 2026". */
export function monthName(month: string) {
  const [y, m] = month.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, 1)).toLocaleDateString("en-GB", { month: "long", year: "numeric", timeZone: "UTC" });
}

/** A deadline's action in words, as the calendar event's title has it. */
export const ACTION_LABEL: Record<DeadlineRow["action"], string> = { cancel_by: "Cancellation deadline", renew_by: "Renewal action due" };
