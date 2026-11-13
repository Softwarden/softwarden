/** Admin › Assistant (capability `assistant`): the settings (on or off, the organization's
 *  Anthropic API key, write-only, and the monthly cost cap), which key calls use, this month's spend against the
 *  effective cap, the model and its price, a refused key, and what is and is never sent; and the usage log, one row per
 *  API call for a month kept in the URL (`?month=YYYY-MM`). Admin only, reads included (they show the spend and the
 *  key's state). Without the capability both pages say so and call nothing. Loaded on first use.
 *
 *  The key is never shown, sent back or kept in the page after a save: the API answers only whether one is set and
 *  since when. Settings are saved under If-Match; a 412 offers the latest version. */
import { useCallback, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, apiWithEtag, CAPABILITY_REQUIRED, Problem, when, type AssistantMonth, type AssistantOutcome, type AssistantSettings, type AssistantUsage } from "@/api";
import { AdminOnly, Breadcrumb, EmptyState, ErrorBanner, ErrorSummary, Field, Loading, PageHead, TableRegion } from "@/components/app";
import { chip, DetailList, DetailRow, GapChip, SuccessNote } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { problemText, useApiQuery } from "@/lib/query";
import { useMe } from "@/lib/session";
import { hasAssistant, usd } from "@/features/discovery/assistant-ui";
import { docLink } from "@/features/discovery/discovery-ui";

const WHAT = "the assistant's settings and usage log: its API key, the monthly cost cap and what each call cost";
/** docs/PERMISSIONS.md's "AI assistant: Anthropic (off by default)" section in the in-app reference. */
export const ASSISTANT_DOC = docLink("ai-assistant-anthropic-off-by-default");

/** The organization lacks the capability (turned off for it): say what the assistant is and that it isn't available. */
function Gate({ title }: { title: string }) {
  return (
    <>
      <PageHead title={title} />
      <div className="max-w-prose rounded-md border border-dashed border-border-strong bg-muted px-4 py-3" data-unavailable="" data-testid="assistant-gate">
        <p className="font-semibold"><span aria-hidden="true">⊘ </span>The assistant isn't available.</p>
        <p className="mt-1">Claude suggests linking, creating or dismissing discovery candidates, with a confidence and a reason, and Editors decide. It is part of every edition, and has been turned off for this organization: ask the installation's operator to turn it back on. While it is off, nothing is ever sent to Anthropic.</p>
        <p className="mt-1"><Link className="underline" to={ASSISTANT_DOC}>What the assistant would send</Link></p>
      </div>
    </>
  );
}

const isGate = (e: unknown) => e instanceof Problem && e.status === 403 && e.type === CAPABILITY_REQUIRED;

// ---- the month against the cap --------------------------------------------------------------------------------------

const monthName = (start: string) => {
  const [y, m] = start.split("-").map(Number);
  return new Intl.DateTimeFormat("en", { month: "long", year: "numeric", timeZone: "UTC" }).format(Date.UTC(y, m - 1, 1));
};

/** Spend against the cap: a bar (decoration, capped at full) and the same in words. */
export function MonthMeter({ month }: { month: AssistantMonth }) {
  const spent = Number(month.to_date_usd);
  const cap = Number(month.cap_usd);
  const pct = cap > 0 ? (spent / cap) * 100 : 0;
  const pctText = pct > 0 && pct < 1 ? "under 1%" : `${Math.round(pct)}%`;
  return (
    <span className="inline-flex flex-wrap items-center gap-2" data-testid="assistant-meter">
      <span aria-hidden="true" className="inline-block h-2 w-28 shrink-0 overflow-hidden rounded-full border border-border-strong bg-muted">
        <span className="block h-full rounded-full bg-primary" style={{ width: `${Math.max(0, Math.min(100, pct))}%` }} />
      </span>
      <span className="tabular">{usd(month.to_date_usd)} of {usd(month.cap_usd)} ({pctText}); {usd(month.remaining_usd)} left</span>
    </span>
  );
}

function CapReached({ month }: { month: AssistantMonth }) {
  if (!month.cap_reached) return <span className="text-muted-foreground">Not this month</span>;
  return (
    <span className="inline-flex flex-wrap items-center gap-2" data-testid="cap-reached">
      <span className={chip({ state: "overdue" })}><span aria-hidden="true">!</span>Cap reached</span>
      <span>{when(month.cap_reached_at)}: a batch stopped because its next call could have passed the cap.</span>
    </span>
  );
}

// ---- settings -------------------------------------------------------------------------------------------------------

export function AssistantSettingsPage() {
  return <AdminOnly title="Assistant" what={WHAT}><SettingsGate /></AdminOnly>;
}

function SettingsGate() {
  const me = useMe();
  return hasAssistant(me) ? <Settings /> : <Gate title="Assistant" />;
}

type Form = { enabled: boolean; apiKey: string; removeKey: boolean; cap: string };
const formOf = (s: AssistantSettings): Form => ({ enabled: s.enabled, apiKey: "", removeKey: false, cap: s.monthly_cap_usd });

function Settings() {
  const [loaded, setLoaded] = useState<{ data: AssistantSettings; etag: string | null } | null>(null);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [form, setForm] = useState<Form | null>(null);
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [stale, setStale] = useState(false);
  const [notice, setNotice] = useState("");
  // A failure that belongs to no field (the network, a 409, a reload after saving): its own alert, not the switch's.
  const [failure, setFailure] = useState("");
  const [saving, setSaving] = useState(false);
  // `live` drops an answer the page no longer wants: the first of the dev server's two mount effects (StrictMode) landed
  // after the form was shown and reset what had been typed, so a cap typed at once was never saved.
  // The same holds for a page left before its read lands.
  const load = useCallback(async (live: () => boolean = () => true) => {
    setLoadError(null);
    try {
      const got = await apiWithEtag<AssistantSettings>("assistant/settings");
      if (!live()) return false;
      setLoaded(got);
      setForm(formOf(got.data));
      return true;
    } catch (e) {
      if (live()) setLoadError(e);
      return false;
    }
  }, []);
  useEffect(() => {
    let live = true;
    void load(() => live);
    return () => { live = false; };
  }, [load]);
  if (isGate(loadError)) return <Gate title="Assistant" />;
  if (loadError && !loaded) {
    return (
      <>
        <PageHead title="Assistant" />
        <ErrorBanner message={problemText(loadError, "Couldn't load the assistant's settings.")} onRetry={() => void load()} />
      </>
    );
  }
  if (!loaded || !form) return <><PageHead title="Assistant" /><Loading what="the assistant's settings" rows={3} /></>;
  const s = loaded.data;
  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (saving || !form) return;
    const body: Record<string, unknown> = {};
    if (form.enabled !== s.enabled) body.enabled = form.enabled;
    if (form.cap.trim() !== s.monthly_cap_usd) body.monthly_cap_usd = form.cap.trim();
    if (form.apiKey.trim()) body.api_key = form.apiKey.trim();
    else if (form.removeKey) body.api_key = null;
    if (!Object.keys(body).length) { setNotice("Nothing changed, so nothing was saved."); return; }
    setSaving(true);
    setErrors({});
    setNotice("");
    setFailure("");
    let saved = false;
    try {
      await api<AssistantSettings>("assistant/settings", { method: "PATCH", body, headers: loaded?.etag ? { "If-Match": loaded.etag } : {} });
      const what = [
        "api_key" in body && (body.api_key === null ? "the key was removed" : "the key was replaced"),
        "enabled" in body && (form.enabled ? "the assistant is on" : "the assistant is off"),
        "monthly_cap_usd" in body && `the cap is ${usd(form.cap.trim())}`,
      ].filter(Boolean).join(", ");
      setNotice(`Saved: ${what}.`);
      saved = true;
    } catch (err) {
      if (err instanceof Problem && err.status === 412) setStale(true);
      else if (err instanceof Problem && Object.keys(err.errors).length) setErrors(err.errors);
      else setFailure(`Not saved: ${problemText(err, "Couldn't save the settings. Try again.")}`);
    } finally {
      setSaving(false);
    }
    if (saved && !(await load())) setFailure("Saved, but the settings couldn't be loaded again, so what this page shows may be out of date. Reload the page.");
  }
  const set = (patch: Partial<Form>) => setForm((f) => (f ? { ...f, ...patch } : f));
  const ceilingApplies = s.api_key_source === "installation" && s.installation_cap_usd !== null && Number(s.installation_cap_usd) < Number(s.monthly_cap_usd);
  return (
    <>
      <PageHead title="Assistant" desc="Claude suggests linking, creating or dismissing new discovery candidates, with a confidence and a reason. Editors decide; every decision is recorded against the suggestion. Calls are paid per use with an Anthropic API key and held under a monthly cap.">
        <Button asChild variant="outline"><Link to="/admin/assistant/usage">Usage log</Link></Button>
      </PageHead>
      <SuccessNote text={notice} />
      {failure && (
        <div className="mb-4 flex max-w-3xl flex-wrap items-center gap-3 rounded-md border border-l-4 border-error-border bg-error-bg px-4 py-3" role="alert" data-testid="settings-failure">
          <strong className="min-w-0 flex-1"><span aria-hidden="true">! </span>{failure}</strong>
          {loadError !== null && <Button type="button" size="sm" variant="outline" onClick={() => void load().then((ok) => ok && setFailure(""))}>Retry</Button>}
        </div>
      )}
      {stale && (
        <div className="mb-4 flex max-w-3xl flex-wrap items-center gap-3 rounded-md border border-l-4 border-error-border bg-error-bg px-4 py-3" role="alert">
          <strong className="min-w-0 flex-1">Someone else changed the assistant's settings after you opened them, so yours weren't saved. Load the latest version to see theirs, then make your change again.</strong>
          <Button type="button" size="sm" variant="outline" onClick={() => { setStale(false); void load(); }}>Load the latest version</Button>
        </div>
      )}
      {s.key_refused_at && (
        <div className="mb-4 max-w-3xl rounded-md border border-l-4 border-error-border bg-error-bg px-4 py-3" role="alert" data-testid="key-refused">
          <p className="font-semibold"><span aria-hidden="true">! </span>The Anthropic API refused the key on {when(s.key_refused_at)}, so the assistant was turned off.</p>
          {s.key_refused_detail && <p className="mt-1">{s.key_refused_detail}</p>}
        </div>
      )}
      <section aria-labelledby="assistant-status" className="mb-5 max-w-3xl rounded-lg border bg-card">
        <h2 id="assistant-status" className="border-b px-4 py-2.5 font-display text-section font-semibold">Status</h2>
        <DetailList>
          <DetailRow label="Assistant">
            {s.enabled ? <span className={chip({ state: "confirmed" })}><span aria-hidden="true">✓</span>On</span> : <span className={chip({ state: "neutral" })}><span aria-hidden="true">–</span>Off</span>}
          </DetailRow>
          <DetailRow label="API key in use">
            <span data-testid="key-source">
              {s.api_key_source === "organization" && <><span className={chip({ state: "confirmed" })}><span aria-hidden="true">✓</span>This organization's own key</span> <span className="text-sm">set on {s.api_key_set_on ?? "an earlier date"}; never shown</span></>}
              {s.api_key_source === "installation" && <><span className={chip({ state: "neutral" })}><span aria-hidden="true">◇</span>The installation's key</span> <span className="text-sm">ANTHROPIC_API_KEY, at most {usd(s.installation_cap_usd)} a month for this organization</span></>}
              {s.api_key_source === "none" && <><GapChip label="No API key" /> <span className="text-sm">Enter one below to turn the assistant on.</span></>}
            </span>
          </DetailRow>
          <DetailRow label="Model">
            <code className="text-sm">{s.model}</code>{" "}
            {s.model_priced && s.prices
              ? <span className="text-sm">{s.prices.input} USD per million input tokens, {s.prices.output} USD per million output tokens (cache reads {s.prices.cache_read}, cache writes {s.prices.cache_write})</span>
              : <><GapChip label="No price for this model" /> <span className="mt-1 block text-sm">Its cost can't be kept under the cap, so the assistant can't run. The operator sets SOFTWARDEN_ASSISTANT_PRICES for it, or a model Softwarden knows.</span></>}
          </DetailRow>
          <DetailRow label={monthName(s.month.start)}><MonthMeter month={s.month} /></DetailRow>
          <DetailRow label="Cap reached"><CapReached month={s.month} /></DetailRow>
        </DetailList>
        {ceilingApplies && (
          <p className="border-t px-4 py-2.5 text-sm">This organization's cap is {usd(s.monthly_cap_usd)}, but on the installation's key it may spend at most {usd(s.installation_cap_usd)} a month, so that is the cap above.</p>
        )}
      </section>
      <section aria-labelledby="assistant-settings" className="mb-5 max-w-3xl rounded-lg border bg-card px-4 py-4">
        <h2 id="assistant-settings" className="mb-3 font-display text-section font-semibold">Settings</h2>
        <form onSubmit={save} noValidate aria-labelledby="assistant-settings">
          <ErrorSummary errors={errors} focusOnShow="field" />
          <div className="mb-3">
            <label className="flex min-h-6 items-center gap-2 font-medium">
              <input type="checkbox" role="switch" id="enabled" checked={form.enabled} aria-checked={form.enabled} aria-describedby={errors.enabled ? "enabled-help enabled-error" : "enabled-help"} aria-invalid={errors.enabled ? true : undefined}
                onChange={(e) => set({ enabled: e.target.checked })} className="h-4 w-4 accent-primary" />
              Turn the assistant on
            </label>
            <p id="enabled-help" className="pl-6 text-sm text-muted-foreground">After each discovery run with new or changed candidates, and when an Editor asks. Needs an API key and a priced model.</p>
            {errors.enabled && <p className="pl-6 text-sm text-error-foreground" id="enabled-error">{errors.enabled.join(" ")}</p>}
          </div>
          <Field id="api_key" label={s.api_key_set ? "Replace the API key" : "Anthropic API key"} errors={errors}
            help={s.api_key_set
              ? `Set on ${s.api_key_set_on ?? "an earlier date"}. Leave empty to keep it. It is stored encrypted and never shown.`
              : `Stored encrypted and never shown again.${s.installation_key_available ? ` Without one, calls use the installation's key, at most ${usd(s.installation_cap_usd)} a month.` : ""}`}>
            <Input id="api_key" type="password" value={form.apiKey} autoComplete="new-password" spellCheck={false} onChange={(e) => set({ apiKey: e.target.value, removeKey: false })} />
          </Field>
          {s.api_key_set && (
            <label className="-mt-1 mb-3 flex min-h-6 items-center gap-2 text-sm">
              <input type="checkbox" id="remove_key" checked={form.removeKey} disabled={!!form.apiKey} onChange={(e) => set({ removeKey: e.target.checked })} className="h-4 w-4 accent-primary" />
              Remove the stored key{s.installation_key_available ? " (calls then use the installation's key)" : " (the assistant can't run without one)"}
            </label>
          )}
          <Field id="monthly_cap_usd" label="Monthly cap (USD)" errors={errors} help="Before each call the worst case is reserved; a call that could pass the cap isn't made. Above 0 and up to 10,000, in dollars and cents.">
            <Input id="monthly_cap_usd" inputMode="decimal" className="w-40" value={form.cap} onChange={(e) => set({ cap: e.target.value })} />
          </Field>
          <div className="flex flex-wrap gap-2 border-t pt-4">
            <Button type="submit" disabled={saving}>{saving ? "Saving…" : "Save settings"}</Button>
          </div>
        </form>
      </section>
      <SentAndNever />
    </>
  );
}

/** What a call carries and what it never carries (docs/PERMISSIONS.md, AI assistant). */
function SentAndNever() {
  return (
    <section aria-labelledby="assistant-data" className="max-w-3xl rounded-lg border bg-card px-4 py-4">
      <h2 id="assistant-data" className="mb-2 font-display text-section font-semibold">What is sent to Anthropic</h2>
      <div className="grid gap-4 md:grid-cols-2">
        <div>
          <h3 className="mb-1 font-semibold">Sent, per candidate (5 per call, 50 per batch)</h3>
          <ul className="ml-5 list-disc text-sm">
            <li>Its name, kind and source type</li>
            <li>Description, publisher and homepage (scheme, host and path only), when the source reported them</li>
            <li>Up to 3 existing records it may be: name, kind and name similarity, under a short handle</li>
          </ul>
        </div>
        <div>
          <h3 className="mb-1 font-semibold">Never sent</h3>
          <ul className="ml-5 list-disc text-sm">
            <li>Any ID: records, connectors, tenants, applications, objects</li>
            <li>Owners, credentials, secrets, Entra notes, query strings, the tenant's own domain</li>
            <li>Contracts, costs, people, attachments, custom fields, the audit log, other organizations' data</li>
          </ul>
        </div>
      </div>
      <p className="mt-3 text-sm">Kept per call: the model, token counts, cost and outcome; never the prompt, the answer text or the key. In full: <Link className="underline" to={ASSISTANT_DOC}>permissions reference, AI assistant</Link>.</p>
    </section>
  );
}

// ---- usage log ------------------------------------------------------------------------------------------------------

export function AssistantUsagePage() {
  return <AdminOnly title="Assistant usage log" what={WHAT}><UsageGate /></AdminOnly>;
}

function UsageGate() {
  const me = useMe();
  return hasAssistant(me) ? <Usage /> : <Gate title="Assistant usage log" />;
}

const OUTCOME: Record<AssistantOutcome, { tone: "confirmed" | "pending" | "overdue" | "neutral"; glyph: string }> = {
  ok: { tone: "confirmed", glyph: "✓" }, pending: { tone: "neutral", glyph: "◔" }, refusal: { tone: "neutral", glyph: "⊘" },
  max_tokens: { tone: "pending", glyph: "◐" }, invalid: { tone: "pending", glyph: "!" }, rate_limited: { tone: "pending", glyph: "◐" },
  key_refused: { tone: "overdue", glyph: "!" }, bad_request: { tone: "overdue", glyph: "!" }, api_error: { tone: "overdue", glyph: "!" },
  connection_error: { tone: "pending", glyph: "◐" }, abandoned: { tone: "neutral", glyph: "○" },
};
const MONTH = /^(\d{4})-(0[1-9]|1[0-2])$/;
const shift = (month: string, by: number) => {
  const [, y, m] = MONTH.exec(month)!;
  const d = new Date(Date.UTC(Number(y), Number(m) - 1 + by, 1));
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`;
};
const count = (k: number) => k.toLocaleString("en");

function Usage() {
  const [params, setParams] = useSearchParams();
  const month = params.get("month") ?? "";
  // A month in the URL that isn't YYYY-MM (01 to 12) is never asked for: the page says so instead.
  const badMonth = month !== "" && !MONTH.test(month);
  const q = useApiQuery<AssistantUsage>(`assistant/usage${month ? `?month=${encodeURIComponent(month)}` : ""}`, { enabled: !badMonth });
  const setMonth = (m: string) => setParams((p) => { const n = new URLSearchParams(p); if (m) n.set("month", m); else n.delete("month"); return n; });
  // The month box gives "" while a month is half typed: that keeps the month shown rather than dropping ?month=.
  const pickMonth = (m: string) => { if (MONTH.test(m)) setMonth(m); };
  const data = q.data;
  const shown = data ? data.month.start.slice(0, 7) : MONTH.test(month) ? month : "";
  const trail = [{ to: "/admin/assistant", label: "Assistant" }];
  if (isGate(q.error)) return <Gate title="Assistant usage log" />;
  return (
    <>
      <Breadcrumb trail={trail} current="Usage log" />
      <PageHead title="Assistant usage log" desc="One row per Anthropic API call: its tokens, the worst case reserved before it against what it cost, and what it came to. Newest first, in the organization's timezone." />
      <form className="mb-4 flex flex-wrap items-end gap-3" aria-label="Choose a month" onSubmit={(e) => e.preventDefault()}>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="usage-month">Month</Label>
          <Input id="usage-month" type="month" className="w-48" value={shown} onChange={(e) => pickMonth(e.target.value)} />
        </div>
        {shown && <Button type="button" variant="outline" onClick={() => setMonth(shift(shown, -1))}>Previous month</Button>}
        {shown && <Button type="button" variant="outline" onClick={() => setMonth(shift(shown, 1))}>Next month</Button>}
        {month && <Button type="button" variant="outline" onClick={() => setMonth("")}>This month</Button>}
      </form>
      {badMonth && <ErrorBanner message={`“${month}” isn't a month. Use YYYY-MM with a month from 01 to 12, such as 2026-11.`} />}
      {q.isError && <ErrorBanner message={problemText(q.error, "Couldn't load the usage log.")} onRetry={() => void q.refetch()} />}
      {!data && !q.isError && !badMonth && <Loading what="the usage log" rows={4} />}
      {data && (
        <>
          <p className="mb-3 flex flex-wrap items-center gap-2" data-testid="usage-total">
            <span className="font-semibold">{monthName(data.month.start)}:</span> <MonthMeter month={data.month} />
            {data.month.cap_reached && <span className={chip({ state: "overdue" })}><span aria-hidden="true">!</span>Cap reached {when(data.month.cap_reached_at)}</span>}
          </p>
          <p className="mb-3 text-sm text-muted-foreground">The cap shown is today's. Calls in progress count at their reserved worst case.</p>
          {data.truncated && (
            <p className="mb-3 rounded-md border border-state-pending-border bg-state-pending px-3 py-2 text-state-pending-foreground" data-testid="usage-truncated">
              <span aria-hidden="true">◐ </span>The log is cut off: it shows the newest {count(data.results.length)} calls of this month. The total above counts them all.
            </p>
          )}
          {data.results.length === 0 ? (
            <EmptyState title={`No calls in ${monthName(data.month.start)}`}>The assistant made no Anthropic API calls this month. A call appears here when a batch runs, after a discovery run or when an Editor asks.</EmptyState>
          ) : (
            <TableRegion label="Assistant usage table">
              <Table>
                <TableCaption className="sr-only">Assistant calls in {monthName(data.month.start)}, newest first</TableCaption>
                <TableHeader><TableRow>
                  <TableHead scope="col">When</TableHead><TableHead scope="col">Outcome</TableHead><TableHead scope="col" className="text-right">Candidates</TableHead>
                  <TableHead scope="col" className="text-right">Input tokens</TableHead><TableHead scope="col" className="text-right">Output tokens</TableHead>
                  <TableHead scope="col" className="text-right">Cache reads</TableHead><TableHead scope="col" className="text-right">Cache writes</TableHead>
                  <TableHead scope="col" className="text-right">Estimated (reserved)</TableHead><TableHead scope="col" className="text-right">Actual cost</TableHead>
                  <TableHead scope="col">Model</TableHead>
                </TableRow></TableHeader>
                <TableBody>
                  {data.results.map((r) => {
                    const o = OUTCOME[r.outcome] ?? OUTCOME.pending;
                    return (
                      <TableRow key={r.id} data-outcome={r.outcome}>
                        <TableHead scope="row" className="tabular font-normal text-foreground">{when(r.created_at)}</TableHead>
                        <TableCell><span className={chip({ state: o.tone })}><span aria-hidden="true">{o.glyph}</span>{r.outcome_label}</span></TableCell>
                        <TableCell className="tabular text-right">{r.candidate_count}</TableCell>
                        <TableCell className="tabular text-right">{count(r.input_tokens)}</TableCell>
                        <TableCell className="tabular text-right">{count(r.output_tokens)}</TableCell>
                        <TableCell className="tabular text-right">{count(r.cache_read_input_tokens)}</TableCell>
                        <TableCell className="tabular text-right">{count(r.cache_creation_input_tokens)}</TableCell>
                        <TableCell className="tabular text-right">{usd(r.estimated_cost_usd)}</TableCell>
                        <TableCell className="tabular text-right">{r.outcome === "pending" ? <>{usd(r.cost_usd)} <span className="text-muted-foreground">(reserved)</span></> : usd(r.cost_usd)}</TableCell>
                        <TableCell><code className="text-xs">{r.model}</code></TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </TableRegion>
          )}
        </>
      )}
    </>
  );
}
