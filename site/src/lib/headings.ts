export interface Heading {
  depth: number;
  slug: string;
  text: string;
}

/** Headings Astro collected while rendering a markdown entry (typed loosely by the content layer). */
export function headingsOf(entry: { rendered?: { metadata?: Record<string, unknown> } }): Heading[] {
  const h = entry.rendered?.metadata?.headings;
  return Array.isArray(h) ? (h as Heading[]) : [];
}
