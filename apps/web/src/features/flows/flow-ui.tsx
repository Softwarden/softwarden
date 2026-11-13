/** The data-flow map's words and chips (capability `data_flows`), shared by the map (`/data-flows`), the
 *  Needs attention "Data flows" queue and the integration page. Kept small: Needs attention is in the first bundle.
 *  A risk is a chip with a glyph and a short name ("No DPA"); the API's sentence says why beside it. */
import { DATA_CATEGORIES, type DataCategory } from "@/api";
import { chip, ChipGroup } from "@/components/sw";
import type { Schemas } from "@/lib/openapi";

export type FlowRisk = Schemas["DataFlowRiskEnum"];
export type Classification = Schemas["DataClassificationEnum"];

export const RISKS: { value: FlowRisk; label: string }[] = [
  { value: "no_dpa", label: "No DPA" },
  { value: "no_auth_method", label: "No auth method" },
  { value: "down_classification", label: "Down-classified" },
];
const RISK_LABEL = Object.fromEntries(RISKS.map((r) => [r.value, r.label])) as Record<FlowRisk, string>;

export const CLASSIFICATION_LABEL: Record<Classification, string> = {
  public: "Public", internal: "Internal", confidential: "Confidential", restricted: "Restricted",
};

/** One risk: "! No DPA", in the pending tone (the glyph and the words carry it, not the colour). */
export function RiskChip({ code }: { code: FlowRisk }) {
  return <span className={chip({ state: "pending" })} data-risk={code}><span aria-hidden="true">!</span>{RISK_LABEL[code] ?? code}</span>;
}

/** A flow's risks once each, in the API's order. */
export function RiskChips({ risks }: { risks: { code: FlowRisk }[] }) {
  const codes = [...new Set(risks.map((r) => r.code))];
  return <ChipGroup>{codes.map((c) => <RiskChip key={c} code={c} />)}</ChipGroup>;
}

export const categoryLabel = (c: DataCategory | string) => DATA_CATEGORIES.find((x) => x.value === c)?.label ?? c;

/** "Contains personal data" as a neutral chip with a glyph, or nothing. */
export function PersonalDataMark({ on }: { on: boolean | undefined }) {
  if (!on) return null;
  return <span className={chip({ state: "neutral" })} data-personal=""><span aria-hidden="true">●</span>Contains personal data</span>;
}

/** Data categories as neutral chips, or `empty` when there are none. */
export function CategoryChips({ categories, empty }: { categories: (DataCategory | string)[] | undefined; empty?: React.ReactNode }) {
  if (!categories?.length) return <>{empty ?? <span className="text-muted-foreground">None recorded</span>}</>;
  return <ChipGroup>{categories.map((c) => <span key={c} className={chip({ state: "neutral" })}>{categoryLabel(c)}</span>)}</ChipGroup>;
}
