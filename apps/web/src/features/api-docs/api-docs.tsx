/** API reference and guide: every signed-in account may open them (the schema is readable by any member, and
 *  scripts and the MCP server act with a member's role). The reference renders this installation's OpenAPI schema
 *  (GET /api/v1/schema?format=json) with Scalar, loaded on first use in its own chunk; the guide is docs/API.md as
 *  shipped with this version, bundled as text and rendered without HTML (markdown-doc.tsx), so its links work on any
 *  installation without the repository at hand. Both pages are their own chunks. */
import { Link } from "react-router-dom";
// `@docs` is the repository's docs/ folder in development (vite.config.ts) and /docs in the Docker build stage.
import guide from "@docs/API.md?raw";
import { Breadcrumb, ErrorBanner, lazyPart, LazyPart, PageHead } from "@/components/app";
import { Button } from "@/components/ui/button";
import { MarkdownDoc } from "@/features/discovery/markdown-doc";
import { problemText, useApiQuery } from "@/lib/query";
import { useMe } from "@/lib/session";

const ScalarReference = lazyPart(() => import("@/features/api-docs/scalar-reference").then((m) => m.default));

export const REFERENCE_PAGE = "/admin/api";
export const GUIDE_PAGE = "/admin/api/guide";
const SCHEMA = "schema?format=json";

/** The guide's sections people look for first; each anchor is a heading of docs/API.md. */
const TOPICS: { anchor: string; label: string; about: string }[] = [
  { anchor: "signing-in", label: "Signing in and API tokens", about: "sessions, bearer tokens, read-only tokens and roles" },
  { anchor: "errors-problem-details", label: "Problem types", about: "every error as RFC 9457 problem details, by type" },
  { anchor: "rate-limits", label: "Rate limits", about: "per token and per user, reads and writes apart, Retry-After" },
  { anchor: "revisions-etag-and-if-match", label: "Revisions", about: "ETag and If-Match on edits" },
  { anchor: "lists-and-pagination", label: "Lists and pagination", about: "keyset cursors, ordering and page sizes" },
  { anchor: "the-composite-namespace", label: "Composite endpoints", about: "compact answers for scripts and the MCP server" },
  { anchor: "webhooks", label: "Webhooks", about: "events, the signed request, retries and turning an endpoint off" },
  { anchor: "verifying-a-webhook", label: "Verifying a webhook", about: "checking X-Softwarden-Signature in Python or Node" },
  { anchor: "generated-client", label: "Generated client", about: "TypeScript types from the schema" },
];

function Topics() {
  const me = useMe();
  return (
    <section aria-labelledby="api-topics" className="mb-5 max-w-4xl rounded-lg border bg-card px-4 py-3">
      <h2 id="api-topics" className="mb-2 font-display text-section font-semibold">Start here</h2>
      <ul className="m-0 grid list-none gap-x-6 gap-y-1.5 p-0 sm:grid-cols-2">
        {TOPICS.map((t) => (
          <li key={t.anchor} className="text-sm">
            <Link to={`${GUIDE_PAGE}#${t.anchor}`} className="font-semibold underline">{t.label}</Link>
            <span className="text-muted-foreground">: {t.about}</span>
          </li>
        ))}
      </ul>
      <p className="mt-3 text-sm text-muted-foreground">
        The MCP server, which lets an AI assistant read the catalog with an API token, is described in docs/MCP.md in the Softwarden repository.
        {me.role === "admin"
          ? <> Create a token for a script or the MCP server in <Link className="underline" to="/admin/tokens">API tokens</Link>.</>
          : " Ask an Admin for an API token: Admins create them in Administration › API tokens."}
      </p>
    </section>
  );
}

export function ApiReferencePage() {
  const schema = useApiQuery<Record<string, unknown>>(SCHEMA, { staleTime: 5 * 60_000 });
  return (
    <>
      <PageHead title="API reference" desc={<>Every route of this installation's REST API at /api/v1/, from its OpenAPI schema. It is read with your session and nothing is sent anywhere else. How to sign in, errors and limits are in the <Link className="underline" to={GUIDE_PAGE}>API guide</Link>.</>}>
        <Button asChild variant="outline"><a href="/api/v1/schema?format=json" download="softwarden-openapi.json">Download the schema (JSON)</a></Button>
      </PageHead>
      <Topics />
      {schema.isError && <ErrorBanner message={problemText(schema.error, "Couldn't load the API schema.")} onRetry={() => void schema.refetch()} />}
      {!schema.data && !schema.isError && <p className="text-muted-foreground" aria-live="polite">Loading the API schema…</p>}
      {schema.data && <LazyPart what="the API reference"><ScalarReference schema={schema.data} /></LazyPart>}
    </>
  );
}

export function ApiGuidePage() {
  return (
    <>
      <Breadcrumb trail={[{ to: REFERENCE_PAGE, label: "API reference" }]} current="API guide" />
      <PageHead title="API guide" desc={<>What every route shares: signing in, tokens, problem types, rate limits, revisions, pagination and webhooks. This is docs/API.md as shipped with this version; file paths in it refer to the source code. Each route is in the <Link className="underline" to={REFERENCE_PAGE}>API reference</Link>.</>} />
      <MarkdownDoc source={guide} testId="api-guide" tableLabel="API guide" />
    </>
  );
}
