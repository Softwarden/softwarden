/** A record's End of life panel (capability `eol`), on the Overview of an application or a component,
 *  loaded on first use. It shows the record detail's `end_of_life` block: the endoflife.date product, the version and
 *  release cycle (and where each came from), the derived status, the dates, and the instances that run a version of
 *  their own. Editors and Admins link the record to a product (PATCH .../end-of-life), change it, and unlink it
 *  (DELETE, behind a checkbox, since it removes the instances' versions too). Viewers read it. */
import { useEffect, useRef, useState } from "react";
import { api, apiWithEtag, Problem } from "@/api";
import { ErrorBanner, ErrorSummary, Field, Select } from "@/components/app";
import { Combobox } from "@/components/combobox";
import { DetailList, DetailRow, Panel } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { useReturnFocus } from "@/components/app";
import { orgDate } from "@/components/org-banners";
import { ExtLink } from "@/features/catalog/detail";
import { useMe } from "@/lib/session";
import { useTypedQuery, type Schemas } from "@/lib/openapi";
import { eolDetail, EolStatusChip } from "./eol-ui";
import { StaleBanner } from "@/features/money/contract-ui";
import { ifMatch, isStaleWrite, staleDeleteText, useOpenedRecord } from "@/lib/if-match";

type Block = Schemas["RecordEndOfLife"];
type Props = {
  kind: "application" | "component"; id: string; name: string; version?: string;
  block: Block | null | undefined; write: boolean; onChanged: (notice: string) => void;
};

const IconCalendar = (
  <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <rect x="2" y="3" width="12" height="11" rx="1.5" /><path d="M2 6.5h12M5.5 1.5v3M10.5 1.5v3M6 10l1.5 1.5L10.5 8.5" />
  </svg>
);
const base = (kind: Props["kind"], id: string) => `${kind === "application" ? "applications" : "components"}/${id}/end-of-life`;

export function EndOfLifePanel({ kind, id, name, version, block, write, onChanged }: Props) {
  const me = useMe();
  const [editing, setEditing] = useState(false);
  // End-of-life reminders link here (`#h-eol`); the panel loads after the page, so it scrolls itself into view and takes focus.
  useEffect(() => {
    if (window.location.hash !== "#h-eol") return;
    const heading = document.getElementById("h-eol");
    if (!heading) return;
    heading.tabIndex = -1;
    heading.scrollIntoView({ block: "start" });
    heading.focus();
  }, []);
  const [unlinking, setUnlinking] = useState(false);
  const focusLink = () => document.getElementById("eol-link");
  // Linking replaces "Link to endoflife.date" with Change, and unlinking the reverse, once the record reloads (after the
  // dialog has closed): focus then moves to the button that took the opener's place.
  const focusNext = useRef<"eol-change" | "eol-link" | null>(null);
  const tracked = !!block;
  useEffect(() => {
    const want = focusNext.current;
    if (want && (want === "eol-change") === tracked) {
      focusNext.current = null;
      document.getElementById(want)?.focus();
    }
  }, [tracked]);
  const action = write && (block
    ? <span className="flex gap-2">
        <Button id="eol-change" size="sm" variant="outline" onClick={() => setEditing(true)}>Change<span className="sr-only"> end-of-life tracking</span></Button>
        <Button size="sm" variant="outline" onClick={() => setUnlinking(true)}>Unlink<span className="sr-only"> from endoflife.date</span></Button>
      </span>
    : <Button id="eol-link" size="sm" variant="outline" onClick={() => setEditing(true)}>Link to endoflife.date</Button>);
  return (
    <Panel id="h-eol" icon={IconCalendar} title="End of life" action={action}>
      {!block ? (
        <p className="px-4 py-3 text-muted-foreground" data-testid="eol-untracked">
          Not tracked. {write ? "Link it to its endoflife.date product to see when its version reaches end of life." : "Editors and Admins can link it to its endoflife.date product."}
        </p>
      ) : (
        <>
          <DetailList>
            <DetailRow label="Status">
              <span className="flex flex-wrap items-center gap-2"><EolStatusChip row={block} />{eolDetail(block) && <span className="text-sm text-muted-foreground">{eolDetail(block)}</span>}</span>
              <span className="mt-1 block text-sm text-muted-foreground">{block.why}</span>
            </DetailRow>
            <DetailRow label="Product">
              {block.product_label}
              {block.product_link && <span className="block text-sm"><ExtLink url={block.product_link} /></span>}
              {block.product_renamed_to && <span className="block text-sm text-state-pending-foreground"><span aria-hidden="true">◐ </span>Renamed to {block.product_renamed_to} on endoflife.date: change the product to keep its dates current.</span>}
            </DetailRow>
            <DetailRow label="Version">
              {block.version || <span className="text-muted-foreground">Not recorded</span>}
              {block.version_source === "application" && <span className="block text-sm text-muted-foreground">From the application's Version field</span>}
            </DetailRow>
            <DetailRow label="Release cycle">
              {block.cycle || <span className="text-muted-foreground">Not known</span>}
              {block.cycle && <span className="block text-sm text-muted-foreground">{block.cycle_source === "explicit" ? "Set by hand" : "Worked out from the version"}</span>}
            </DetailRow>
            <DetailRow label="End of life">{block.eol_date ? <span className="tabular">{block.eol_date}</span> : <span className="text-muted-foreground">No date</span>}</DetailRow>
            {block.support_until && <DetailRow label="Active support until"><span className="tabular">{block.support_until}</span></DetailRow>}
            {block.extended_support_until && <DetailRow label="Extended support until"><span className="tabular">{block.extended_support_until}</span></DetailRow>}
            <DetailRow label="Latest release">{block.latest ? <>{block.latest}{block.latest_date && <span className="text-muted-foreground"> ({block.latest_date})</span>}</> : <span className="text-muted-foreground">—</span>}</DetailRow>
            <DetailRow label="Checked">{block.checked_at ? <span className="tabular">{orgDate(block.checked_at, me.organization.timezone)}</span> : <span className="text-muted-foreground">Not yet</span>}</DetailRow>
          </DetailList>
          {block.instances.length > 0 && (
            <div className="border-t px-4 py-3">
              <h3 className="mb-1 text-sm font-semibold">Instances with their own version</h3>
              <ul className="grid gap-1.5">
                {block.instances.map((i) => (
                  <li key={i.id} className="flex flex-wrap items-center gap-2 text-sm">
                    <span className="font-semibold">{i.instance?.name}</span>
                    <span>{i.product_label}{i.cycle ? ` ${i.cycle}` : ""}{i.version ? ` (version ${i.version})` : ""}</span>
                    <EolStatusChip row={i} />
                  </li>
                ))}
              </ul>
            </div>
          )}
          {!write && <p className="border-t px-4 py-2 text-sm text-muted-foreground">Editors and Admins can change or unlink it.</p>}
        </>
      )}
      {write && <TrackDialog open={editing} kind={kind} id={id} name={name} version={version} block={block ?? null}
        onClose={() => setEditing(false)} onDone={(text) => { if (!block) focusNext.current = "eol-change"; setEditing(false); onChanged(text); }} />}
      {write && block && <UnlinkDialog open={unlinking} kind={kind} id={id} name={name} fallback={focusLink}
        onClose={() => setUnlinking(false)} onDone={(text) => { focusNext.current = "eol-link"; setUnlinking(false); onChanged(text); }} />}
    </Panel>
  );
}

/** Field errors keyed by this dialog's input IDs (`eol-product`), so the summary's links reach them. */
const keyed = (errors: Record<string, string[]>) => Object.fromEntries(Object.entries(errors).map(([k, v]) => [`eol-${k}`, v]));

function TrackDialog({ open, kind, id, name, version, block, onClose, onDone }: {
  open: boolean; kind: Props["kind"]; id: string; name: string; version?: string; block: Block | null; onClose: () => void; onDone: (text: string) => void;
}) {
  const returnFocus = useReturnFocus(open, () => document.getElementById("eol-change") ?? document.getElementById("eol-link"));
  const [product, setProduct] = useState("");
  const [productLabel, setProductLabel] = useState("");
  const [ver, setVer] = useState("");
  const [cycle, setCycle] = useState("");
  const [text, setText] = useState("");
  const [search, setSearch] = useState("");
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [failure, setFailure] = useState("");
  const [busy, setBusy] = useState(false);
  // If-Match: a tracked record's tracking is read with its ETag as the dialog opens and the form starts from that
  // read, not the page's copy; the save sends the ETag. An untracked record's save is a create (no If-Match).
  const [current, setCurrent] = useState<{ data: Block | null; etag: string | null } | null>(null);
  const [stale, setStale] = useState(false);
  function startFrom(from: Block | null) {
    setProduct(from?.product ?? ""); setProductLabel(from?.product_label ?? "");
    setVer(from?.version_source === "tracking" ? from.version : ""); setCycle(from?.cycle_source === "explicit" ? from.cycle : "");
  }
  /** The tracking as stored now, with its ETag; null data when it isn't tracked (404). */
  async function readTracking() {
    try {
      return await apiWithEtag<Block>(base(kind, id));
    } catch (e) {
      if (e instanceof Problem && e.status === 404) return { data: null, etag: null };
      throw e;
    }
  }
  useEffect(() => {
    if (!open) { setCurrent(null); return; }
    setErrors({}); setFailure(""); setText(""); setStale(false);
    if (!block) { setCurrent({ data: null, etag: null }); startFrom(null); return; }
    let live = true;
    readTracking().then((got) => { if (live) { setCurrent(got); startFrom(got.data); } },
      (e) => { if (live) setFailure(`${e instanceof Problem ? e.detail : "Couldn't load the tracking."} Close this and try again.`); });
    return () => { live = false; };
  }, [open, block]); // eslint-disable-line react-hooks/exhaustive-deps
  async function loadLatest() {
    try {
      const got = await readTracking();
      setCurrent(got); startFrom(got.data); setStale(false); setErrors({}); setFailure("");
    } catch (e) {
      setFailure(`${e instanceof Problem ? e.detail : "Couldn't load the latest version."} Try again.`);
    }
  }
  useEffect(() => { const t = setTimeout(() => setSearch(text.trim()), 250); return () => clearTimeout(t); }, [text]);
  const found = useTypedQuery("end-of-life/products", { query: { search } }, { enabled: open, staleTime: 60_000 });
  // A tracked product's cycles are known (the API gives them only for products this organization tracks).
  const detail = useTypedQuery("end-of-life/products/{name}", { path: { name: product || "-" } }, { enabled: open && !!product, staleTime: 60_000, retry: false });
  const releases = product && detail.data?.name === product ? detail.data.releases : [];
  const options = (found.data ?? []).map((p) => ({ id: p.name, label: p.label, hint: [p.name, p.category].filter(Boolean).join(" · ") }));
  if (product && !options.some((o) => o.id === product)) options.unshift({ id: product, label: productLabel || product, hint: product });

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!product) { setErrors({ "eol-product": ["Choose the endoflife.date product."] }); return; }
    if (!current) return;
    setBusy(true); setErrors({}); setFailure("");
    try {
      const was = current.data;
      const body: Record<string, unknown> = { version: ver.trim(), cycle: cycle.trim() || null };
      if (!was || product !== was.product) body.product = product;
      await api(base(kind, id), { method: "PATCH", body, headers: ifMatch(current.etag) });
      onDone(was ? `End-of-life tracking of ${name} changed.` : `${name} is linked to ${productLabel || product} on endoflife.date.`);
    } catch (err) {
      // 412: changed since this opened; 428: someone else linked it meanwhile (this save was a create).
      if (isStaleWrite(err) || (err instanceof Problem && err.status === 428)) setStale(true);
      else if (err instanceof Problem && Object.keys(err.errors).length) setErrors(keyed(err.errors));
      else setFailure(`${err instanceof Problem ? err.detail : "Couldn't save."} Nothing was changed.`);
    } finally {
      setBusy(false);
    }
  }
  const versionHelp = kind === "application"
    ? `Leave empty to use the application's Version field${version ? ` (${version})` : " (empty now)"}.`
    : "The version this component runs, such as 16.4.";
  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o) onClose(); }}>
      <DialogContent className="max-w-lg" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>{block ? `Change end-of-life tracking of ${name}` : `Link ${name} to endoflife.date`}</DialogTitle>
          <DialogDescription>The status and dates come from endoflife.date's public data, refreshed daily.</DialogDescription>
        </DialogHeader>
        <form id="eol-form" onSubmit={(e) => void save(e)} noValidate aria-label="End-of-life tracking">
          {stale && <StaleBanner what="record's end-of-life tracking" onLoad={() => void loadLatest()} />}
          {failure && <ErrorBanner message={failure} />}
          <ErrorSummary errors={errors} focusOnShow="field" />
          <Field id="eol-product" label="Product (required)" errors={errors} help="Search endoflife.date's products by name, such as PostgreSQL or Ubuntu.">
            <Combobox id="eol-product" options={options} value={product} listLabel="Product suggestions" placeholder="Search products…"
              onChange={(v) => { setProduct(v); setProductLabel(options.find((o) => o.id === v)?.label ?? ""); setCycle(""); }}
              remote={{ onQueryChange: setText, busy: found.isFetching }} />
          </Field>
          {found.isError && <p className="-mt-2 mb-3 text-sm text-error-foreground" role="alert">Couldn't search the products. Try again.</p>}
          <Field id="eol-version" label="Version" errors={errors} help={versionHelp}>
            <Input id="eol-version" value={ver} onChange={(e) => setVer(e.target.value)} autoComplete="off" />
          </Field>
          <Field id="eol-cycle" label="Release cycle" errors={errors} help="Leave empty to work it out from the version (16.4 belongs to cycle 16).">
            {releases.length > 0 ? (
              <Select id="eol-cycle" value={cycle} onChange={(e) => setCycle(e.target.value)}>
                <option value="">Work it out from the version</option>
                {releases.map((r) => <option key={r.name} value={r.name}>{r.label || r.name}{r.eol_date ? ` (end of life ${r.eol_date})` : ""}</option>)}
                {cycle && !releases.some((r) => r.name === cycle) && <option value={cycle}>{cycle}</option>}
              </Select>
            ) : <Input id="eol-cycle" value={cycle} onChange={(e) => setCycle(e.target.value)} autoComplete="off" />}
          </Field>
        </form>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
          <Button type="submit" form="eol-form" disabled={busy || !current}>{busy ? "Saving…" : !current ? "Loading…" : block ? "Save" : "Link"}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function UnlinkDialog({ open, kind, id, name, fallback, onClose, onDone }: {
  open: boolean; kind: Props["kind"]; id: string; name: string; fallback: () => HTMLElement | null; onClose: () => void; onDone: (text: string) => void;
}) {
  const returnFocus = useReturnFocus(open, fallback);
  const [sure, setSure] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (open) { setSure(false); setError(""); } }, [open]);
  // Read with its ETag as the confirmation opens; the unlink sends it as If-Match.
  const opened = useOpenedRecord(open ? base(kind, id) : null);
  async function remove() {
    if (!sure) { setError("Tick the box to confirm."); return; }
    setBusy(true); setError("");
    try {
      await api(base(kind, id), { method: "DELETE", headers: ifMatch(await opened.etag()) });
      onDone(`${name} is no longer tracked on endoflife.date.`);
    } catch (e) {
      setError(isStaleWrite(e)
        ? staleDeleteText("tracking", "remove")
        : `${e instanceof Problem ? e.detail : "Couldn't unlink it."} Nothing was changed.`);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o) onClose(); }}>
      <DialogContent className="max-w-md" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>Stop tracking {name}'s end of life?</DialogTitle>
          <DialogDescription>Its product, version and cycle are removed, with any versions recorded for its instances. It leaves the end-of-life calendar and Needs attention.</DialogDescription>
        </DialogHeader>
        {error && <ErrorBanner message={error} />}
        <label className="flex min-h-8 items-center gap-2 text-sm">
          <input type="checkbox" checked={sure} onChange={(e) => setSure(e.target.checked)} className="h-4 w-4 accent-primary" />
          Remove the end-of-life tracking of {name}
        </label>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
          <Button type="button" variant="destructive" disabled={busy} onClick={() => void remove()}>Stop tracking {name}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
