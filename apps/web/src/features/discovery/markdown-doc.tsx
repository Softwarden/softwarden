/** A repository document shown inside the app (docs/PERMISSIONS.md on the permissions reference, docs/API.md on the API
 *  guide): the file is bundled into its page's chunk as text at build time and rendered by the small Markdown reader
 *  (markdown.ts) as React elements only (headings with anchors, paragraphs, lists, tables, code blocks, inline code,
 *  bold and links), never HTML. The file's "#" title is left out (the page's h1 says it), and the section a link's
 *  #anchor names is scrolled to and focused, so keyboard and screen reader users start in it. */
import { Fragment, useEffect } from "react";
import { useLocation } from "react-router-dom";
import { TableRegion } from "@/components/app";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { slug } from "@/features/discovery/discovery-ui";
import { inline, parse } from "@/features/discovery/markdown";

/** Inline Markdown as React nodes (markdown.ts decides what is a link; anything else is text). */
function Inline({ text }: { text: string }) {
  return (
    <>
      {inline(text).map((tok, k) => {
        if (tok.t === "code") return <code key={k} className="rounded-sm bg-muted px-1 text-[0.92em] [overflow-wrap:anywhere]">{tok.text}</code>;
        if (tok.t === "bold") return <strong key={k}><Inline text={tok.text} /></strong>;
        if (tok.t === "link") {
          return tok.external
            ? <a key={k} href={tok.href} target="_blank" rel="noreferrer" className="underline">{tok.text}<span className="sr-only"> (opens in a new tab)</span></a>
            : <a key={k} href={tok.href} className="underline">{tok.text}</a>;
        }
        return <Fragment key={k}>{tok.text}</Fragment>;
      })}
    </>
  );
}

function Heading({ level, text }: { level: number; text: string }) {
  const id = slug(text.replace(/`/g, ""));
  // The file's "#" title is the page's h1, so "##" sections are h2 and so on.
  const Tag = (`h${Math.min(Math.max(level, 2), 6)}`) as "h2";
  const size = level <= 2 ? "mt-8 font-display text-section font-semibold" : "mt-5 font-semibold";
  return <Tag id={id} tabIndex={-1} className={`${size} mb-2 scroll-mt-4 outline-none`}><Inline text={text} /></Tag>;
}

export function MarkdownDoc({ source, testId, tableLabel = "Reference" }: { source: string; testId?: string; tableLabel?: string }) {
  const { hash } = useLocation();
  const blocks = parse(source);
  useEffect(() => {
    if (!hash) return;
    const el = document.getElementById(decodeURIComponent(hash.slice(1)));
    if (el) { el.scrollIntoView({ block: "start" }); el.focus({ preventScroll: true }); }
  }, [hash]);
  const body = blocks[0]?.t === "h" && blocks[0].level === 1 ? blocks.slice(1) : blocks;
  let tables = 0;
  // Running text breaks a long identifier (403 Authentication_RequestFromNonPremiumTenantOrB2CTenant) rather than widen
  // the page at 320 px; tables scroll in their own region.
  return (
    <article className="max-w-4xl leading-relaxed" data-testid={testId}>
      {body.map((b, i) => {
        switch (b.t) {
          case "h": return <Heading key={i} level={b.level} text={b.text} />;
          case "p": return <p key={i} className="mb-3 max-w-prose [overflow-wrap:anywhere]"><Inline text={b.text} /></p>;
          case "ul": return <ul key={i} className="mb-3 ml-5 max-w-prose list-disc [overflow-wrap:anywhere]">{b.items.map((t, j) => <li key={j} className="mb-1"><Inline text={t} /></li>)}</ul>;
          case "ol": return <ol key={i} className="mb-3 ml-5 max-w-prose list-decimal [overflow-wrap:anywhere]">{b.items.map((t, j) => <li key={j} className="mb-1"><Inline text={t} /></li>)}</ol>;
          case "code": return <pre key={i} className="mb-3 overflow-x-auto rounded-md border bg-muted px-3 py-2 text-xs" tabIndex={0} aria-label="Code"><code>{b.text}</code></pre>;
          case "hr": return <hr key={i} className="my-6 border-border" />;
          case "table": {
            tables += 1;
            return (
              <div key={i} className="mb-4">
                <TableRegion label={`${b.caption || tableLabel} table ${tables}`}>
                  <Table>
                    <TableCaption className="sr-only">{b.caption || tableLabel}</TableCaption>
                    <TableHeader><TableRow>{b.head.map((h, j) => <TableHead key={j} scope="col" className="whitespace-normal"><Inline text={h} /></TableHead>)}</TableRow></TableHeader>
                    <TableBody>
                      {b.rows.map((r, j) => (
                        <TableRow key={j} className="align-top">{r.map((c, n) => <TableCell key={n} className="min-w-28 whitespace-normal text-sm"><Inline text={c} /></TableCell>)}</TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </TableRegion>
              </div>
            );
          }
        }
      })}
    </article>
  );
}
