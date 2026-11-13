/** Data quality on records: the completeness score as a small bar with its
 *  percentage in text, and a record's failing and excepted checks as chips, each failing one with "Add exception" where
 *  the rule allows it (Editors and Admins). Small and eager; the exception dialog is loaded when first opened. */
import { useState } from "react";
import { Link } from "react-router-dom";
import { SEVERITY_LABEL, type Me, type QualityRule, type RecordQuality } from "@/api";
import { lazyPart, LazyPart } from "@/components/app";
import { chip, GapChip, Panel } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { useApiQuery } from "@/lib/query";
import { canWrite, useMe } from "@/lib/session";
import type { RecordKind } from "@/lib/kinds";

export const hasQuality = (me: Me) => me.capabilities.includes("data_quality");

/** Every rule with its status here (any role reads them); shared by lists, detail pages and the data quality page. */
export function useQualityRules(enabled = true) {
  return useApiQuery<QualityRule[]>("data-quality/rules", { staleTime: 30_000, enabled });
}

/** The score as a bar and its percentage. The bar is decoration; the number is the value (never colour alone). */
export function CompletenessBar({ score, label }: { score: number; label?: string }) {
  const value = Math.max(0, Math.min(100, score));
  return (
    <span className="inline-flex items-center gap-2" data-completeness={value}>
      <span aria-hidden="true" className="inline-block h-2 w-16 shrink-0 overflow-hidden rounded-full border border-border-strong bg-muted">
        <span className="block h-full rounded-full bg-primary" style={{ width: `${value}%` }} />
      </span>
      <span className="tabular text-sm">{label && <span className="sr-only">{label} </span>}{value}%</span>
    </span>
  );
}

/** A check marked not applicable: a neutral chip with its own glyph, never the gap's orange. */
export function ExceptedChip({ label }: { label: string }) {
  return <span className={chip({ state: "neutral" })}><span aria-hidden="true">⊘</span>{label}<span className="font-normal text-muted-foreground">· not applicable</span></span>;
}

const STATUS_CHIP: Record<QualityRule["status"], { glyph: string; label: string; state: "confirmed" | "neutral" | "unreviewed" }> = {
  on: { glyph: "✓", label: "On", state: "confirmed" },
  off: { glyph: "○", label: "Off", state: "unreviewed" },
  unavailable: { glyph: "–", label: "Unavailable", state: "neutral" },
};

/** A rule's status here, as a chip with a glyph and its word. */
export function RuleStatusChip({ status }: { status: QualityRule["status"] }) {
  const s = STATUS_CHIP[status];
  return <span className={chip({ state: s.state })}><span aria-hidden="true">{s.glyph}</span>{s.label}</span>;
}

const ExceptionDialog = lazyPart(() => import("./exception-dialog").then((m) => m.ExceptionDialog));

const IconQuality = <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M3 13V8M8 13V3M13 13v-7" /><path d="M1.5 13.5h13" /></svg>;

/** The Data quality card of a record's Overview tab: the failing checks (with Add exception) and those marked not
 *  applicable. The completeness score and failing count are shown once, in the record's status strip. */
export function RecordQualityPanel({ record, quality, onChanged }: {
  record: { id: string; kind: RecordKind; name: string }; quality: RecordQuality; onChanged: () => void;
}) {
  const me = useMe();
  const write = canWrite(me);
  const rules = useQualityRules();
  const byId = new Map((rules.data ?? []).map((r) => [r.id, r]));
  const [adding, setAdding] = useState<string | null>(null);
  const [opened, setOpened] = useState(false);
  const [notice, setNotice] = useState("");
  const exceptable = (id: string) => { const r = byId.get(id); return !!r && r.exceptable && r.status === "on"; };
  const failing = quality.failing;
  return (
    <Panel id="h-quality" icon={IconQuality} title="Data quality"
      action={<Link to={`/data-quality?tab=exceptions&record=${record.id}`} className="text-sm">Exceptions for this record</Link>}>
      <div className="flex flex-col gap-3 px-4 py-3">
        {/* The score and failing count are in the page's Record status strip; this card lists the checks themselves. */}
        {failing.length === 0 && <p className="text-sm text-success-foreground">✓ Passes every check that applies{quality.excepted.length > 0 ? ", apart from those marked not applicable" : ""}.</p>}
        <p className="sr-only" role="status">{notice}</p>
        {notice && <p className="text-sm text-success-foreground" aria-hidden="true">✓ {notice}</p>}
        {failing.length > 0 && (
          <ul aria-label="Failing checks" className="m-0 flex list-none flex-col gap-2 p-0">
            {failing.map((f) => (
              <li key={f.id} className="flex flex-wrap items-center gap-2">
                <GapChip label={f.label} />
                <span className="text-sm text-muted-foreground">{SEVERITY_LABEL[f.severity]} severity</span>
                {write && exceptable(f.id) && (
                  <Button size="sm" variant="outline" onClick={() => { setOpened(true); setAdding(f.id); }}>
                    Add exception<span className="sr-only"> for {f.label}</span>
                  </Button>
                )}
              </li>
            ))}
          </ul>
        )}
        {quality.excepted.length > 0 && (
          <ul aria-label="Checks marked not applicable" className="m-0 flex list-none flex-wrap gap-2 p-0">
            {quality.excepted.map((x) => <li key={x.id}><ExceptedChip label={x.label} /></li>)}
          </ul>
        )}
      </div>
      {opened && (
        <LazyPart what="the exception form">
          <ExceptionDialog open={adding !== null} onClose={() => setAdding(null)} record={record} rule={adding ?? undefined}
            onDone={(text) => { setAdding(null); setNotice(text); onChanged(); }} />
        </LazyPart>
      )}
    </Panel>
  );
}
