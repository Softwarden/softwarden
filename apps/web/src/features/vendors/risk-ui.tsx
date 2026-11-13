/** The vendor risk file's words and chips, shared by the vendor page's Risk tab, the risk overview and the
 *  documents register. Labels mirror catalog/models.py (VendorRiskTier, QuestionnaireStatus, AssuranceType, DpaStatus,
 *  RiskDocumentKind); statuses are derived by the API and shown as a glyph plus words, never colour alone. */
import { Link, useLocation } from "react-router-dom";
import { chip, CriticalityMark, GapChips } from "@/components/sw";
import type { Schemas } from "@/lib/openapi";

export type RiskProfile = Schemas["VendorRiskProfile"];
export type RiskDocument = Schemas["VendorRiskDocument"];
export type RiskOverviewRow = Schemas["VendorRiskOverviewRow"];
export type RiskTier = Schemas["CriticalityEnum"];
export type DocumentKind = Schemas["RiskDocumentKindEnum"];
export type DocumentStatus = Schemas["RiskDocumentStatusEnum"];
export type AssuranceStatus = Schemas["AssuranceStatusEnum"];
export type AssuranceType = Schemas["AssuranceTypeEnum"];
export type QuestionnaireStatus = Schemas["QuestionnaireStatusEnum"];
export type DpaStatus = Schemas["DpaStatusEnum"];
export type RiskGapCode = Schemas["VendorRiskGapEnum"];

export const TIERS: { value: RiskTier; label: string }[] = [
  { value: "critical", label: "Critical" }, { value: "high", label: "High" }, { value: "medium", label: "Medium" }, { value: "low", label: "Low" },
];
export const QUESTIONNAIRE: { value: QuestionnaireStatus; label: string }[] = [
  { value: "not_requested", label: "Not requested" }, { value: "requested", label: "Requested" }, { value: "received", label: "Received" },
  { value: "reviewed", label: "Reviewed" }, { value: "not_required", label: "Not required" },
];
export const ASSURANCE_TYPES: { value: AssuranceType; label: string }[] = [
  { value: "soc2_type1", label: "SOC 2 Type I" }, { value: "soc2_type2", label: "SOC 2 Type II" },
  { value: "iso27001", label: "ISO 27001 certificate" }, { value: "other", label: "Other assurance report" },
];
export const DPA_STATUSES: { value: DpaStatus; label: string }[] = [
  { value: "yes", label: "Signed" }, { value: "no", label: "Not signed" }, { value: "not_applicable", label: "Not applicable" },
];
export const DOCUMENT_KINDS: { value: DocumentKind; label: string }[] = [
  { value: "soc2", label: "SOC 2 report" }, { value: "iso27001", label: "ISO 27001 certificate" }, { value: "pen_test", label: "Penetration test report" },
  { value: "questionnaire", label: "Security questionnaire" }, { value: "dpa", label: "Data processing agreement" },
  { value: "insurance", label: "Insurance certificate" }, { value: "other", label: "Other" },
];
/** Kinds the API refuses without an expiry (an undated report would count as current for ever). */
export const DATED_KINDS: DocumentKind[] = ["soc2", "iso27001"];
export const GAP_CHOICES: { value: RiskGapCode; label: string }[] = [
  { value: "no_current_assurance", label: "No current SOC 2 or equivalent" }, { value: "dpa_missing", label: "No signed DPA" },
];

const labelOf = <T extends string>(list: { value: T; label: string }[], v: T | null | undefined, fallback = "") =>
  (v ? list.find((x) => x.value === v)?.label ?? v : fallback);
export const kindLabel = (v: DocumentKind) => labelOf(DOCUMENT_KINDS, v);
export const questionnaireLabel = (v: QuestionnaireStatus) => labelOf(QUESTIONNAIRE, v);
export const assuranceTypeLabel = (v: AssuranceType | null) => labelOf(ASSURANCE_TYPES, v);
export const dpaLabel = (v: DpaStatus | null) => labelOf(DPA_STATUSES, v);

/** The risk tier as the criticality mark (text plus a diamond); not assessed is the unreviewed chip. */
export function TierMark({ tier }: { tier: RiskTier | null | undefined }) {
  if (!tier) return <span className={chip({ state: "unreviewed" })}><span aria-hidden="true">○</span>Not assessed</span>;
  return <CriticalityMark value={tier} suffix=" risk" />;
}

const DOC_STATUS: Record<DocumentStatus, { state: "confirmed" | "pending" | "overdue"; glyph: string; label: string }> = {
  current: { state: "confirmed", glyph: "✓", label: "Current" },
  expiring: { state: "pending", glyph: "◐", label: "Expiring" },
  expired: { state: "overdue", glyph: "!", label: "Expired" },
};

/** A document's derived status: ✓ Current, ◐ Expiring, ! Expired. */
export function DocumentStatusChip({ status }: { status: DocumentStatus }) {
  const s = DOC_STATUS[status];
  return <span className={chip({ state: s.state })} data-status={status}><span aria-hidden="true">{s.glyph}</span>{s.label}</span>;
}

/** The profile's assurance report: the document statuses, or ○ No report recorded until it has a type and an issue date. */
export function AssuranceStatusChip({ status }: { status: AssuranceStatus }) {
  if (status === "missing") return <span className={chip({ state: "unreviewed" })}><span aria-hidden="true">○</span>No report recorded</span>;
  return <DocumentStatusChip status={status} />;
}

/** A superseded item: a later one of its family replaces it, so it raises no reminder and isn't counted. */
export function ReplacedChip() {
  return <span className={chip({ state: "neutral" })} data-replaced=""><span aria-hidden="true">↻</span>Replaced</span>;
}

/** The vendor's risk gaps as the gap chips ownership gaps use, in the API's words. */
export function RiskGaps({ gaps }: { gaps: { code: string; label: string }[] }) {
  return <GapChips gaps={gaps.map((g) => g.label)} />;
}

/** "in 12 days", "Expires today", "Expired 3 days ago" for a document's `days_left`. */
export function daysText(days: number | null | undefined): string {
  if (days === null || days === undefined) return "";
  if (days === 0) return "Expires today";
  if (days > 0) return `in ${days} ${days === 1 ? "day" : "days"}`;
  return `Expired ${-days} ${days === -1 ? "day" : "days"} ago`;
}

/** The vendor's page, open on its Risk tab. */
export const riskPath = (vendorId: string) => `/vendors/${vendorId}?tab=risk`;

export function VendorRiskSections() {
  const sections = [{ to: "/vendor-risk", label: "Overview" }, { to: "/vendor-risk/documents", label: "Documents" }];
  const here = useLocation().pathname;
  return (
    <nav aria-label="Vendor risk sections" className="mb-4 inline-flex overflow-hidden rounded-md border border-border-strong bg-card">
      {sections.map((s) => (
        <Link key={s.to} to={s.to} aria-current={here === s.to ? "page" : undefined}
          className="flex h-8 items-center px-3 text-[13px] text-muted-foreground no-underline aria-[current=page]:bg-accent aria-[current=page]:font-semibold aria-[current=page]:text-accent-foreground [&+&]:border-l [&+&]:border-border-strong">
          {s.label}
        </Link>
      ))}
    </nav>
  );
}

/** Without the capability: a dashed block saying so, never a silent absence or a zero. */
export function VendorRiskUnavailable() {
  return (
    <p className="rounded-md border border-dashed border-border-strong bg-muted px-4 py-3 text-muted-foreground" data-testid="vendor-risk-unavailable">
      <span aria-hidden="true">⊘ </span>The vendor risk file isn't available in this organization's edition.
    </p>
  );
}
