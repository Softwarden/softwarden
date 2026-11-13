/** Global search results, shared by the command palette and the /search page (both loaded on first use).
 *
 *  - `resultHref`: where a result opens. Record kinds go to the record's page (src/lib/kinds.ts, the API's `path` is the
 *    same route); vendors to the vendor's page; people to People & Teams; an attachment to its parent's Attachments tab
 *    (a record's tab, or the vendor's attachments dialog).
 *  - `Snippet`: where the query matched, rendered as text with <mark> around the matching segments. Every segment is a
 *    React text child, never HTML, so nothing stored in a record (an <img onerror> in a description) can become
 *    markup. */
import { Building2, FileSignature, FileText, User, type LucideIcon } from "lucide-react";
import type { SearchKind, SearchResult, SearchSegment } from "@/api";
import { attachmentsPage } from "@/features/attachments/paths";
import { isCatalogKind, KIND_LABEL, KIND_PLURAL, recordPath } from "@/lib/kinds";

/** Kinds in the order the API groups them, with their words. */
export const SEARCH_KINDS: { kind: SearchKind; one: string; many: string }[] = [
  { kind: "application", one: KIND_LABEL.application, many: KIND_PLURAL.application },
  { kind: "component", one: KIND_LABEL.component, many: KIND_PLURAL.component },
  { kind: "ai_agent", one: KIND_LABEL.ai_agent, many: KIND_PLURAL.ai_agent },
  { kind: "integration", one: KIND_LABEL.integration, many: KIND_PLURAL.integration },
  { kind: "vendor", one: "Vendor", many: "Vendors" },
  { kind: "person", one: "Person", many: "People" },
  { kind: "attachment", one: "Attachment", many: "Attachments" },
  { kind: "contract", one: "Contract", many: "Contracts" },
];
export const searchKindWords = (kind: string) => SEARCH_KINDS.find((k) => k.kind === kind) ?? { kind, one: kind, many: kind };

/** The capability a kind needs (the API refuses a gated kind with 403). */
const NEEDS: Partial<Record<SearchKind, string>> = { ai_agent: "ai_agents", attachment: "attachments", contract: "contracts" };
export const searchableKinds = (capabilities: string[]) => SEARCH_KINDS.filter((k) => !NEEDS[k.kind] || capabilities.includes(NEEDS[k.kind]!));

export const OTHER_ICON: Partial<Record<SearchKind, LucideIcon>> = { vendor: Building2, person: User, attachment: FileText, contract: FileSignature };

/** The SPA page a result opens, or null when this build has no page for it. */
export function resultHref(r: SearchResult): string | null {
  if (isCatalogKind(r.kind)) return recordPath(r.kind, r.id);
  if (r.kind === "vendor") return `/vendors/${r.id}`;
  if (r.kind === "person") return "/admin/people";
  if (r.kind === "contract") return `/contracts/${r.id}`;
  if (r.kind === "attachment" && r.parent) return attachmentsPage(r.parent.kind, r.parent.id);
  return null;
}

/** Segments as text; matches in <mark>. */
export function Segments({ segments }: { segments: SearchSegment[] }) {
  return <>{segments.map((s, i) => (s.match ? <mark key={i} className="rounded-sm bg-state-pending px-0.5 text-state-pending-foreground">{s.text}</mark> : <span key={i}>{s.text}</span>))}</>;
}

/** "Description: … the matching words …", or nothing for a name match. */
export function Snippet({ result, className }: { result: SearchResult; className?: string }) {
  if (!result.snippet?.segments.length) return null;
  return (
    <span className={className} data-testid="search-snippet">
      <span className="text-muted-foreground">{result.snippet.label}: </span>
      <Segments segments={result.snippet.segments} />
    </span>
  );
}

/** A 429 from search: when to try again. */
export function retryText(seconds?: number) {
  if (!seconds) return "Too many searches for now. Wait a moment and try again.";
  return `Too many searches for now. Try again in ${seconds < 90 ? `${seconds} seconds` : `about ${Math.ceil(seconds / 60)} minutes`}.`;
}
