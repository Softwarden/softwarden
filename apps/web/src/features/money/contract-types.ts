/** Contracts and licence pools: the shapes `GET /contracts`, `/contracts/{id}`, `/contracts/{id}/renew`,
 *  `/license-pools` and `/license-pools/seat-review` return (money/contract_serializers.py), and the words for their
 *  choices. Money is a string with two decimal places next to a three-letter currency; it is never turned into a
 *  floating-point number, only regrouped for display. Imported by the lazily loaded contract and pool pages. */
import type { AutoRenew, Deadline, Decision, Period, PeriodUnit, Person, Renewal, RenewalCycle } from "@/api";

export type Ref = { id: string; name: string };

export type BillingFrequency = "monthly" | "quarterly" | "semiannual" | "annual" | "multi_year" | "one_time" | "usage_based";
export type ContractStatus = "signed" | "active" | "pending_renewal" | "renewed" | "cancelled" | "expired";

/** money.models.BillingFrequency, in the API's order. */
export const BILLING_FREQUENCIES: { value: BillingFrequency; label: string }[] = [
  { value: "monthly", label: "Monthly" }, { value: "quarterly", label: "Quarterly" }, { value: "semiannual", label: "Every six months" },
  { value: "annual", label: "Annual" }, { value: "multi_year", label: "Multi-year" }, { value: "one_time", label: "One-time" },
  { value: "usage_based", label: "Usage-based" },
];
export const FREQUENCY_LABEL = Object.fromEntries(BILLING_FREQUENCIES.map((f) => [f.value, f.label])) as Record<BillingFrequency, string>;
/** What the amount is, per frequency, for the form's help text. */
export const AMOUNT_HELP: Record<BillingFrequency, string> = {
  monthly: "Billed each month.", quarterly: "Billed each quarter.", semiannual: "Billed every six months.", annual: "Billed each year.",
  multi_year: "The whole term's amount; it is spread over the term's months from the dates.", one_time: "Paid once.",
  usage_based: "Your annual estimate.",
};

/** money.models.ContractStatus. Each status is a chip with a glyph and its word. */
export const CONTRACT_STATUSES: { value: ContractStatus; label: string; glyph: string; tone: "proposed" | "active" | "pending" | "neutral" | "retired" }[] = [
  { value: "signed", label: "Signed (not started)", glyph: "◔", tone: "proposed" },
  { value: "active", label: "Active", glyph: "✓", tone: "active" },
  { value: "pending_renewal", label: "Pending renewal", glyph: "◐", tone: "pending" },
  { value: "renewed", label: "Renewed", glyph: "↻", tone: "neutral" },
  { value: "cancelled", label: "Canceled", glyph: "✕", tone: "retired" },
  { value: "expired", label: "Expired", glyph: "○", tone: "retired" },
];
export const STATUS_LABEL = Object.fromEntries(CONTRACT_STATUSES.map((s) => [s.value, s.label])) as Record<ContractStatus, string>;

export type ContractRecordRef = { id: string; name: string; kind: string; lifecycle: string; archived: boolean };
export type ContractLine = {
  id: string; record: ContractRecordRef; allocation_percent: string | null; share_percent: string | null; annual_amount: string | null; created_at: string;
};
export type ContractPool = {
  id: string; sku_name: string; sku_id: string; application: Ref; seats_purchased: number; seats_assigned: number; seats_available: number;
  utilization: number | null; overused: boolean; seat_sync: boolean; last_synced: string | null;
};
export type TermsChange = {
  old_amount: string | null; old_currency: string | null; old_billing_frequency: string | null; old_annualized_cost: string | null;
  old_start_date: string | null; old_end_date: string | null; new_amount: string | null; new_currency: string | null;
  new_billing_frequency: string | null; new_annualized_cost: string | null; new_start_date: string | null; new_end_date: string | null;
};
export type ContractRenewalCycle = RenewalCycle & { terms_change: TermsChange | null };
/** The linked renewal as GET /renewals/{id} gives it, with every earlier cycle. */
export type ContractRenewal = Omit<Renewal, "cycle" | "history"> & { cycle: ContractRenewalCycle; history: ContractRenewalCycle[]; contract: Ref | null };

export type Contract = {
  id: string; name: string; vendor: Ref | null; contract_number: string; po_number: string;
  amount: string | null; currency: string; billing_frequency: BillingFrequency; annualized_cost: string | null; annualization_note: string;
  start_date: string | null; end_date: string | null; term_months: number | null; auto_renew: AutoRenew; notice: Period | null;
  cancellation_deadline: string | null; deadline: Deadline; renewal_owner: Person | null; status: ContractStatus; cost_center: string; notes: string;
  predecessor: Ref | null; successor: Ref | null; renewal_obligation: Ref | null; drives_renewal: boolean; archived: boolean;
  line_count: number; license_pool_count: number; created_at: string; updated_at: string;
  custom_field_data?: Record<string, unknown>;
};
export type ContractDetail = Contract & {
  lines: ContractLine[]; allocated_percent: string; unallocated: { share_percent: string; annual_amount: string | null } | null;
  license_pools: ContractPool[]; renewal: ContractRenewal | null;
};
export type ContractPreview = { annualized_cost: string | null; annualization_note: string; term_months: number | null; deadline: Deadline };

export type RenewDefaults = {
  name: string; contract_number: string; po_number: string; amount: string | null; currency: string; billing_frequency: BillingFrequency;
  start_date: string | null; end_date: string | null; auto_renew: AutoRenew; notice_value: number | null; notice_unit: PeriodUnit | "";
  notes: string; lines: string[]; pools: string[]; pool_action: "move" | "copy"; overlap_confirmed: boolean;
};
export type RenewTerms = {
  can_renew: boolean; refusal: string | null; renewal: Ref | null;
  open_cycle: { id: string; sequence: number; renewal_date: string | null; decision: Decision | null } | null;
  defaults: RenewDefaults;
};

export type LicensePool = {
  id: string; application: Ref; contract: Ref | null; sku_name: string; sku_id: string; seats_purchased: number; seats_assigned: number;
  seats_available: number; utilization: number | null; overused: boolean; seat_sync: boolean; last_synced: string | null;
  seat_proposals: number; notes: string; created_at: string; updated_at: string; custom_field_data?: Record<string, unknown>;
  /** Seat sync on: the one Entra licences connector reporting the SKU, or null; held when two report it. */
  synced_from?: Ref | null; sync_held?: boolean;
};
export type Utilisation = { seats_purchased: number; seats_assigned: number; seats_available: number; utilization: number | null; overused: boolean };
export type PoolTotals = {
  rows: (Utilisation & { application: Ref & { archived: boolean }; sku_name: string; sku_id: string; pools: number })[];
  total: Utilisation & { pools: number; overused_rows: number };
};
export type SeatField = "seats_purchased" | "seats_assigned";
export type SeatSource = { id: string; display_name: string; connector: Ref; last_seen_at: string };
export type SeatProposal = {
  pool: { id: string; sku_name: string; sku_id: string; application: Ref; contract: Ref | null; seats_purchased: number; seats_assigned: number; seat_sync: boolean; last_synced: string | null };
  observation: SeatSource; observed: { seats_purchased: number | null; seats_assigned: number | null };
  differences: { field: SeatField; current: number; proposed: number }[];
  status: "open" | "dismissed"; dismissal: { reason: string; by: { id: number; name: string; email: string } | null; at: string } | null;
};
export type SeatReview = {
  status: "open" | "dismissed" | "all"; results: SeatProposal[];
  unbound: { observation: SeatSource; sku_id: string; sku_part_number: string; observed: { seats_purchased: number | null; seats_assigned: number | null } }[];
};
export const SEAT_FIELD_LABEL: Record<SeatField, string> = { seats_purchased: "Seats purchased", seats_assigned: "Seats assigned" };

/** "12000.5" or "-3" with thousands separators, kept as text: "12,000.50". With `decimals` 0 (a zero-decimal currency)
 *  a zero fraction is left out: "12,000"; a fraction that isn't zero is still shown, never rounded away. */
export function groupDigits(amount: string, decimals = 2): string {
  const negative = amount.startsWith("-");
  const [whole, fraction = ""] = amount.replace(/^-/, "").split(".");
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  if (decimals === 0 && !/[1-9]/.test(fraction)) return `${negative ? "-" : ""}${grouped}`;
  return `${negative ? "-" : ""}${grouped}${fraction ? `.${fraction.padEnd(2, "0")}` : ".00"}`;
}

const decimalsCache = new Map<string, number>();
/** How many decimals a currency has (JPY, KRW, ISK: 0), from the browser's Intl data; 2 for a code it doesn't know. */
export function currencyDecimals(currency: string): number {
  const code = currency.trim().toUpperCase();
  if (!/^[A-Z]{3}$/.test(code)) return 2;
  let n = decimalsCache.get(code);
  if (n === undefined) {
    try {
      n = new Intl.NumberFormat("en", { style: "currency", currency: code }).resolvedOptions().maximumFractionDigits ?? 2;
    } catch {
      n = 2;
    }
    decimalsCache.set(code, n);
  }
  return n;
}

/** "12,000.00 USD" or "1,500,000 JPY", or null for no amount. */
export const moneyText = (amount: string | null | undefined, currency: string) => (amount == null || amount === "" ? null : `${groupDigits(amount, currencyDecimals(currency))} ${currency}`);

/** A percentage the API sends as a decimal string ("40.00", "33.3333") without trailing zeros: "40", "33.3333". */
export const percentText = (p: string | null | undefined) => (p == null ? "" : p.includes(".") ? p.replace(/0+$/, "").replace(/\.$/, "") : p);

/** Hundredths of a percentage point from "40.5" (exact integer arithmetic, so totals never pick up float error). */
export function hundredths(p: string | null | undefined): number | null {
  if (p == null || p.trim() === "") return null;
  const m = /^(\d{1,3})(?:\.(\d{1,2}))?$/.exec(p.trim());
  if (!m) return null;
  return Number(m[1]) * 100 + Number((m[2] ?? "").padEnd(2, "0"));
}
export const fromHundredths = (n: number) => percentText(`${Math.floor(n / 100)}.${String(n % 100).padStart(2, "0")}`);

/** A money amount as typed: digits with at most two decimal places, no sign. */
export const AMOUNT_PATTERN = /^\d{1,12}(\.\d{1,2})?$/;

export const contractPath = (id: string, tab?: string) => `/contracts/${id}${tab ? `?tab=${tab}` : ""}`;
export const poolPath = (id: string, tab?: string) => `/license-pools/${id}${tab ? `?tab=${tab}` : ""}`;
export const CONTRACTS_CAPABILITY = "contracts";
