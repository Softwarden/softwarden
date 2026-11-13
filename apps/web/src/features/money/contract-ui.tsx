/** Small pieces shared by the contract and licence pool pages: the status chip, money in words, the
 *  utilisation meter and the overuse flag. Every status is a glyph plus its word; colour is never the only signal. */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Problem, when, type Page } from "@/api";
import { Combobox } from "@/components/combobox";
import { Button } from "@/components/ui/button";
import { chip } from "@/components/sw";
import { useApiQuery } from "@/lib/query";
import { cn } from "@/lib/utils";
import { CONTRACT_STATUSES, moneyText, type ContractStatus, type Ref } from "./contract-types";

export function ContractStatusChip({ status }: { status: ContractStatus }) {
  const s = CONTRACT_STATUSES.find((x) => x.value === status);
  if (!s) return <span className={chip({ state: "neutral" })}>{status}</span>;
  return <span className={chip({ state: s.tone })} data-status={status}><span aria-hidden="true">{s.glyph}</span>{s.label}</span>;
}

/** "12,000.00 USD", or a dash (not applicable) when there is no amount. */
export function Money({ amount, currency, className }: { amount: string | null | undefined; currency: string; className?: string }) {
  const text = moneyText(amount, currency);
  return text ? <span className={cn("tabular whitespace-nowrap", className)}>{text}</span> : <span className="text-muted-foreground">—</span>;
}

/** Seats used as a small bar plus the words: "90 of 100 seats (90%)". The bar is decoration (aria-hidden) and is capped
 *  at full; overuse is said in words with its own glyph, never by the bar or its colour. */
export function UtilisationMeter({ purchased, assigned, utilization, className }: {
  purchased: number; assigned: number; utilization: number | null; className?: string;
}) {
  const width = utilization == null ? 0 : Math.max(0, Math.min(100, utilization));
  return (
    <span className={cn("inline-flex flex-wrap items-center gap-2", className)} data-utilization={utilization ?? ""}>
      <span aria-hidden="true" className="inline-block h-2 w-20 shrink-0 overflow-hidden rounded-full border border-border-strong bg-muted">
        <span className="block h-full rounded-full bg-primary" style={{ width: `${width}%` }} />
      </span>
      <span className="tabular text-sm">
        {assigned} of {purchased} {purchased === 1 ? "seat" : "seats"}{utilization != null ? ` (${utilization}%)` : " (none purchased)"}
      </span>
    </span>
  );
}

/** "! Overused by 10 seats": a red-toned chip with a glyph and the count in words. */
export function OverusedChip({ by }: { by: number }) {
  return <span className={chip({ state: "overdue" })} data-overused=""><span aria-hidden="true">!</span>Overused by {by} {by === 1 ? "seat" : "seats"}</span>;
}

/** Whether discovery may overwrite the seats, and when it last did (read-only: only the sync path sets it). */
export function SeatSyncText({ sync, lastSynced }: { sync: boolean; lastSynced: string | null }) {
  if (!sync) return <span>Off<span className="block text-sm text-muted-foreground">Seat counts are entered by hand; a source that disagrees is proposed for review.</span></span>;
  return (
    <span>On<span className="block text-sm text-muted-foreground">{lastSynced ? `Last synced ${when(lastSynced)}.` : "Not synced yet: Entra license discovery sets the counts when it runs."}</span></span>
  );
}

export const RefLink = ({ to, value }: { to: string; value: Ref }) => <Link to={to} className="inline-block min-h-6 leading-6">{value.name}</Link>;

/** A 412 on an If-Match write: what happened, and a button that reloads the page's copy. */
export function StaleBanner({ what, onLoad }: { what: string; onLoad: () => void }) {
  return (
    <div className="mb-4 flex flex-wrap items-center gap-3 rounded-md border border-l-4 border-error-border bg-error-bg px-4 py-3" role="alert">
      <strong className="min-w-0 flex-1">Someone else saved changes to this {what} after you opened it, so yours weren't saved. Load the latest version to see theirs, then make your change again.</strong>
      <Button type="button" size="sm" variant="outline" onClick={onLoad}>Load the latest version</Button>
    </div>
  );
}

export const isStale = (e: unknown) => e instanceof Problem && e.status === 412;

/** Downloads moved to `@/lib/download` (one helper for every export); re-exported for the money pages. */
export { useDownload } from "@/lib/download";

/** Search contracts by name or number (`contracts?q=`, 20 at a time) and pick one, instead of a select capped at a page
 *  of contracts. `value` is the chosen contract's id; its name comes from `known` or, for an id alone (a link's
 *  `?contract=`), from `contracts/{id}`. `clearLabel` offers an entry that clears it ("None", "Any"). */
export function ContractPicker({ id, value, known, onChange, clearLabel, className, listLabel = "Contract suggestions", ...aria }: {
  id: string; value: string; known?: Ref | null; onChange: (c: Ref | null) => void; clearLabel?: string; className?: string; listLabel?: string;
  "aria-invalid"?: boolean; "aria-describedby"?: string;
}) {
  const [text, setText] = useState("");
  const [q, setQ] = useState("");
  useEffect(() => { const t = setTimeout(() => setQ(text), 250); return () => clearTimeout(t); }, [text]);
  const found = useApiQuery<Page<Ref>>(`contracts?page_size=20&ordering=name${q ? `&q=${encodeURIComponent(q)}` : ""}`, { staleTime: 30_000 });
  const needName = !!value && known?.id !== value && !found.data?.results.some((c) => c.id === value);
  const one = useApiQuery<Ref>(`contracts/${value}`, { enabled: needName, staleTime: 60_000, retry: false });
  const chosen: Ref | null = !value ? null : known?.id === value ? known : found.data?.results.find((c) => c.id === value) ?? (one.data ? { id: one.data.id, name: one.data.name } : { id: value, name: one.isError ? "A contract that no longer exists" : "Loading the contract…" });
  const list: Ref[] = found.data?.results.map((c) => ({ id: c.id, name: c.name })) ?? [];
  const options = (chosen && !list.some((c) => c.id === chosen.id) ? [chosen, ...list] : list).map((c) => ({ id: c.id, label: c.name }));
  return (
    <>
      <Combobox id={id} options={options} value={value} className={className} placeholder="Search contracts…" listLabel={listLabel} clearLabel={clearLabel}
        onChange={(v) => onChange(v ? (options.find((o) => o.id === v) ? { id: v, name: options.find((o) => o.id === v)!.label } : null) : null)}
        remote={{ onQueryChange: setText, busy: found.isFetching }} {...aria} />
      {found.isError && <p className="mt-1 text-sm text-error-foreground" role="alert">Couldn't search contracts. <button type="button" className="underline" onClick={() => void found.refetch()}>Retry</button></p>}
    </>
  );
}
