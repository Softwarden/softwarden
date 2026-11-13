/** Search results page: `/search?q=&kinds=&include_archived=&limit=`, everything in the URL. Results
 *  come from `GET /api/v1/search`, grouped by kind with each kind's count; "Show all N" asks for that kind alone with
 *  the API's largest page (`kinds=<kind>&limit=100`). Typo matches say "Showing close matches", words the search
 *  couldn't use are listed as "Ignored", and a 429 says when to try again. Loaded on first use. */
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Problem, type SearchResponse, type SearchResult } from "@/api";
import { EmptyState, ErrorBanner, Loading, PageHead } from "@/components/app";
import { chip, KindTile, LifecycleChip } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { isCatalogKind } from "@/lib/kinds";
import { problemText, useApiQuery } from "@/lib/query";
import { useMe } from "@/lib/session";
import { OTHER_ICON, resultHref, retryText, searchableKinds, searchKindWords, Snippet } from "./results";

const LIMIT_ALL = 100;

export function SearchPage() {
  const me = useMe();
  const [params, setParams] = useSearchParams();
  const q = (params.get("q") ?? "").trim();
  const kindsParam = params.get("kinds") ?? "";
  const kinds = kindsParam ? kindsParam.split(",").filter(Boolean) : [];
  const archived = params.get("include_archived") === "true";
  const limit = params.get("limit") ?? "";
  const offered = searchableKinds(me.capabilities);

  const request = new URLSearchParams({ q });
  if (kinds.length) request.set("kinds", kinds.join(","));
  if (archived) request.set("include_archived", "true");
  if (limit) request.set("limit", limit);
  const search = useApiQuery<SearchResponse>(`search?${request.toString()}`, { enabled: q.length > 0, staleTime: 30_000 });

  function update(changes: Record<string, string>) {
    setParams((cur) => {
      const next = new URLSearchParams(cur);
      for (const [k, v] of Object.entries(changes)) { if (v) next.set(k, v); else next.delete(k); }
      return next;
      // Typing the search replaces the history entry; any other change pushes one, so Back undoes it.
    }, { replace: Object.keys(changes).every((k) => k === "q") });
  }
  const toggleKind = (k: string) => {
    const next = kinds.includes(k) ? kinds.filter((x) => x !== k) : [...kinds, k];
    update({ kinds: offered.map((o) => o.kind).filter((o) => next.includes(o)).join(","), limit: "" });
  };

  const [text, setText] = useState(q);
  useEffect(() => { setText(q); }, [q]);
  const filtered = kinds.length > 0 || archived || !!limit;
  const clear = () => update({ kinds: "", include_archived: "", limit: "" });
  const data = search.data;
  const total = data ? data.groups.reduce((n, g) => n + g.count, 0) : 0;
  const error = search.error instanceof Problem ? search.error : null;
  const queryError = error?.status === 400 ? (error.errors.q ?? []).join(" ") : "";

  return (
    <>
      <PageHead title="Search" docTitle={q ? `Search: ${q}` : "Search"}
        desc="Applications, components, AI agents, integrations, vendors, people and attachments: names, descriptions, purposes, vendors, tags, models and text custom fields." />
      <form role="search" className="mb-4 flex flex-wrap items-end gap-3" onSubmit={(e) => { e.preventDefault(); update({ q: text.trim(), limit: "" }); }}>
        <div className="flex w-full max-w-md flex-col gap-1.5">
          <Label htmlFor="search-q">Search everything</Label>
          <Input id="search-q" type="search" value={text} onChange={(e) => setText(e.target.value)} placeholder="A name, a vendor, a word from a description"
            aria-invalid={queryError ? true : undefined} aria-describedby={queryError ? "search-q-error" : undefined} />
          {queryError && <span id="search-q-error" className="text-sm text-error-foreground">{queryError}</span>}
        </div>
        <Button type="submit">Search</Button>
      </form>
      <div className="mb-4 flex flex-wrap items-center gap-x-4 gap-y-2">
        <div role="group" aria-label="Kinds" className="flex flex-wrap items-center gap-1.5">
          <span className="mr-1 text-sm font-semibold" aria-hidden="true">Kinds:</span>
          {offered.map((k) => (
            <button key={k.kind} type="button" aria-pressed={kinds.includes(k.kind)} onClick={() => toggleKind(k.kind)}
              className="inline-flex h-7 items-center gap-1 rounded-full border border-border-strong px-2.5 text-[13px] text-muted-foreground aria-pressed:border-primary aria-pressed:bg-accent aria-pressed:font-semibold aria-pressed:text-accent-foreground">
              {kinds.includes(k.kind) && <span aria-hidden="true">✓</span>}{k.many}
            </button>
          ))}
        </div>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={archived} onChange={(e) => update({ include_archived: e.target.checked ? "true" : "" })} />
          Include archived records, inactive people and their attachments
        </label>
        {filtered && <Button size="sm" variant="outline" onClick={clear}>Clear filters</Button>}
      </div>

      {!q && (
        <EmptyState title="Search the whole catalog">
          Type a name, a vendor, or a word from a description, then press Enter. Every word must match; the last one may be the start of a word. Ctrl+K searches as you type from any page.
        </EmptyState>
      )}
      {q && search.isPending && <Loading what="results" rows={3} />}
      {q && error && !queryError && (
        <ErrorBanner message={error.status === 429 ? retryText(error.retryAfter) : `${problemText(error, "Couldn't search.")} Nothing was changed.`} onRetry={() => void search.refetch()} />
      )}
      {q && !error && search.isError && <ErrorBanner message="Couldn't search. Check your connection and try again." onRetry={() => void search.refetch()} />}
      {data && (
        <div aria-busy={search.isFetching || undefined}>
          <p className="mb-3 text-sm text-muted-foreground" role="status">
            {total === 0 ? `Nothing matches “${data.query}”.` : `${total} ${total === 1 ? "result" : "results"} for “${data.query}”.`}
          </p>
          {data.fuzzy && <p className="mb-3 rounded-md border border-l-4 border-info-border bg-info-bg px-3 py-2" data-testid="search-fuzzy"><span aria-hidden="true">ⓘ </span>Showing close matches: nothing matched every word exactly.</p>}
          {data.ignored.length > 0 && <p className="mb-3 text-sm text-muted-foreground" data-testid="search-ignored">Ignored: {data.ignored.join(" ")} (nothing to match in {data.ignored.length === 1 ? "it" : "them"}).</p>}
          {total === 0 && (
            <EmptyState title={`No results for “${data.query}”`} action={filtered ? <Button variant="outline" onClick={clear}>Clear filters</Button> : undefined}>
              {filtered ? "Nothing matches with these filters. Clear them to search every kind." : "Try fewer words, the start of a name, or a vendor's name."}
            </EmptyState>
          )}
          {data.groups.filter((g) => g.count > 0).map((g) => {
            const words = searchKindWords(g.kind);
            const more = g.count > g.results.length;
            const all = new URLSearchParams({ q: data.query, kinds: g.kind, limit: String(LIMIT_ALL) });
            if (archived) all.set("include_archived", "true");
            return (
              <section key={g.kind} aria-labelledby={`results-${g.kind}`} className="mb-5 overflow-hidden rounded-lg border bg-card">
                <h2 id={`results-${g.kind}`} className="flex flex-wrap items-center gap-2 border-b px-4 py-2.5 font-display text-section font-semibold">
                  {words.many} <span className="tabular rounded-sm bg-muted px-1.5 text-xs font-semibold">{g.count}<span className="sr-only"> {g.count === 1 ? "result" : "results"}</span></span>
                </h2>
                <ul className="m-0 list-none p-0">
                  {g.results.map((r) => <ResultRow key={r.id} r={r} />)}
                </ul>
                {more && (
                  <p className="border-t px-4 py-2 text-sm">
                    <Link to={`/search?${all.toString()}`} className="underline">Show all {g.count} {words.many.toLowerCase().replace(/^ai /, "AI ")}</Link>
                    {g.count > LIMIT_ALL && <span className="text-muted-foreground"> (the first {LIMIT_ALL}; add words to narrow it)</span>}
                  </p>
                )}
              </section>
            );
          })}
        </div>
      )}
    </>
  );
}

function ResultRow({ r }: { r: SearchResult }) {
  const href = resultHref(r);
  const Icon = OTHER_ICON[r.kind];
  return (
    <li className="flex items-start gap-3 border-b border-border-subtle px-4 py-3 last:border-0" data-kind={r.kind}>
      {isCatalogKind(r.kind) ? <KindTile kind={r.kind} className="mt-0.5" /> : Icon && (
        <span className="mt-0.5 inline-grid size-6 shrink-0 place-items-center rounded-md bg-muted text-icon" aria-hidden="true"><Icon className="size-3.5" /></span>
      )}
      <div className="min-w-0 flex-1">
        <p className="flex flex-wrap items-center gap-2">
          {href ? <Link to={href} className="font-semibold [overflow-wrap:anywhere]">{r.name}</Link> : <span className="font-semibold [overflow-wrap:anywhere]">{r.name}</span>}
          {r.lifecycle && <LifecycleChip value={r.lifecycle} />}
          {r.archived && <span className={chip({ state: "neutral" })}>{r.kind === "person" ? "Inactive" : "Archived"}</span>}
        </p>
        {r.parent && <p className="text-sm text-muted-foreground">On {searchKindWords(r.parent.kind).one.toLowerCase().replace(/^ai /, "AI ")} {r.parent.name}</p>}
        <Snippet result={r} className="mt-0.5 block text-sm [overflow-wrap:anywhere]" />
      </div>
    </li>
  );
}
