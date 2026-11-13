/** Relationship types: how a link is put in words, from the API's labels, never from a hard-coded map.
 *
 *  A link reads "<source> <name> <target>" ("Portal depends on Sync") and, from the target's side,
 *  "<target> <inverse_name> <source>" ("Sync is needed by Portal"). Core types (depends_on, implements, replaces) have
 *  labels an Admin may rename; added types have any slug. Every relationship carries its `relationship_type`; the full
 *  list (for pickers, and for snapshot rows that carry only the slug) is `GET relationship-types`. */
import type { RelationshipTypeBrief, RelationshipTypeDef } from "@/api";
import { useApiQuery } from "@/lib/query";

/** How a type's name reads between two names: "Depends on" reads "depends on". The first letter is lower-cased only
 *  when the second isn't a capital, so "SAML trust for" stays as written (the API's `phrase_of`, so the SPA and the
 *  audit log, digest and staleness reasons agree). Without a name, the slug in words. */
export function phraseOf(name: string | undefined | null, slug = "") {
  if (!name) return slug.replace(/_/g, " ");
  if (name.length > 1 && name[1] === name[1].toUpperCase() && name[1] !== name[1].toLowerCase()) return name;
  return name.charAt(0).toLowerCase() + name.slice(1);
}

/** The label read from the target's side. An added type may leave it blank: then the name marked "(from)", as the
 *  record map labels it. */
export const inverseOf = (t: Pick<RelationshipTypeBrief, "name" | "inverse_name">) => t.inverse_name || `${t.name} (from)`;

/** The organization's types, sorted for pickers: dependency types first (depends_on leading), then the rest by name,
 *  with implements last. Cached for a minute; Admin writes invalidate it. */
export function useRelationshipTypes() {
  return useApiQuery<RelationshipTypeDef[]>("relationship-types", { staleTime: 60_000 });
}

export function sortTypes<T extends RelationshipTypeBrief>(types: T[]): T[] {
  const rank = (t: T) => (t.slug === "depends_on" ? 0 : t.is_dependency ? 1 : t.slug === "implements" ? 4 : t.slug === "replaces" ? 2 : 3);
  return [...types].sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name));
}

/** A slug's name from the list, else the slug in words (a snapshot row of a type deleted since, or while loading). */
export function nameOfSlug(types: RelationshipTypeBrief[] | undefined, slug: string) {
  return types?.find((t) => t.slug === slug)?.name ?? "";
}
