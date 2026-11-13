"""Rendering a notification for each channel. The result is stored on the Delivery (`content`), so a retry or a
resend sends exactly what was queued, and it never holds a secret.

A message is a heading, an optional intro, a list of items (`{"title", "detail", "url"}`), an optional footer and an
optional link back to Softwarden:

- email: plain text and an HTML part with the same content (inline styles only; values escaped);
- Teams: an Adaptive Card 1.2 in the Workflows webhook envelope (Microsoft Learn, 2026-11-05: `{"type": "message",
  "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": null, "content": ...}]}`).
  Markdown only, never HTML; no Action.Submit (Action.OpenUrl only). The whole request body stays under Teams' 28 KB:
  long texts are cut and the item list is truncated with "and N more";
- webhook: `{"event", "data"}`; the envelope (id, timestamp) and the signature are added when sent (notify/channels.py).
"""

from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any, TypedDict

from django.utils.html import escape

from core.input import WEB_ADDRESS

TEAMS_LIMIT = 28 * 1024  # bytes of the request body (Microsoft Learn)
TEAMS_BUDGET = TEAMS_LIMIT - 1024  # headroom for the HTTP layer's own counting
TITLE_MAX = 200
DETAIL_MAX = 500
CARD_SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"


class Item(TypedDict, total=False):
    title: str
    detail: str
    url: str


def _cut(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ---- email -------------------------------------------------------------------------------------------------------


def email_content(
    heading: str,
    items: list[Item],
    intro: str = "",
    footer: str = "",
    link: str = "",
    link_label: str = "Open Softwarden",
) -> dict[str, str]:
    """{"text", "html"} for one email."""
    text = [heading, ""]
    if intro:
        text += [intro, ""]
    for item in items:
        line = f"  • {item.get('title', '')}"
        if item.get("detail"):
            line += f" — {item['detail']}"
        text.append(line)
        if item.get("url"):
            text.append(f"    {item['url']}")
    if items:
        text.append("")
    if link:
        text.append(f"{link_label}: {link}")
    if footer:
        text.append(footer)
    html = [
        '<div style="font-family:Segoe UI,Arial,sans-serif;font-size:15px;line-height:1.5;color:#141a24">',
        f'<h1 style="font-size:18px;margin:0 0 12px">{escape(heading)}</h1>',
    ]
    if intro:
        html.append(f"<p>{escape(intro)}</p>")
    if items:
        html.append("<ul>")
        for item in items:
            title: str = escape(item.get("title", ""))
            if item.get("url"):
                title = f'<a href="{escape(item["url"])}">{title}</a>'
            detail = f" — {escape(item['detail'])}" if item.get("detail") else ""
            html.append(f"<li>{title}{detail}</li>")
        html.append("</ul>")
    tail = []
    if link:
        tail.append(f'<a href="{escape(link)}">{escape(link_label)}</a>')
    if footer:
        tail.append(escape(footer))
    if tail:
        html.append(f'<p style="color:#535e6e">{"<br>".join(tail)}</p>')
    html.append("</div>")
    return {"text": "\n".join(text).rstrip() + "\n", "html": "".join(html)}


def email_table_content(
    heading: str,
    columns: list[str],
    rows: list[tuple[str, str, list[str]]],
    intro: str = "",
    more: str = "",
    footer: str = "",
    link: str = "",
    link_label: str = "Open Softwarden",
) -> dict[str, str]:
    """{"text", "html"} for an email that is a table (saved view subscriptions): `columns` are the headings after the
    first (the row's name), each row is (name, url, cells). `more` follows the table ("and 12 more"). Values are
    escaped in the HTML part; the text part lists each row with its cells as "Heading: value"."""
    text = [heading, ""]
    if intro:
        text += [intro, ""]
    for name, url, cells in rows:
        details = "; ".join(f"{h}: {v}" for h, v in zip(columns[1:], cells, strict=False))
        text.append(f"  • {name}" + (f" — {details}" if details else ""))
        if url:
            text.append(f"    {url}")
    if rows:
        text.append("")
    if more:
        text += [more, ""]
    if link:
        text.append(f"{link_label}: {link}")
    if footer:
        text.append(footer)
    cell = 'style="text-align:left;padding:4px 8px;border-bottom:1px solid #d5dae1;vertical-align:top"'
    html = [
        '<div style="font-family:Segoe UI,Arial,sans-serif;font-size:15px;line-height:1.5;color:#141a24">',
        f'<h1 style="font-size:18px;margin:0 0 12px">{escape(heading)}</h1>',
    ]
    if intro:
        html.append(f"<p>{escape(intro)}</p>")
    if rows:
        html.append('<table style="border-collapse:collapse;font-size:14px"><thead><tr>')
        html += [f'<th scope="col" {cell}>{escape(h)}</th>' for h in columns]
        html.append("</tr></thead><tbody>")
        for name, url, cells in rows:
            title: str = escape(name)
            if url:
                title = f'<a href="{escape(url)}">{title}</a>'
            html.append(f'<tr><th scope="row" {cell}>{title}</th>')
            html += [f"<td {cell}>{escape(v)}</td>" for v in cells]
            html.append("</tr>")
        html.append("</tbody></table>")
    if more:
        html.append(f"<p>{escape(more)}</p>")
    tail = []
    if link:
        tail.append(f'<a href="{escape(link)}">{escape(link_label)}</a>')
    if footer:
        tail.append(escape(footer))
    if tail:
        html.append(f'<p style="color:#535e6e">{"<br>".join(tail)}</p>')
    html.append("</div>")
    return {"text": "\n".join(text).rstrip() + "\n", "html": "".join(html)}


# ---- Teams -------------------------------------------------------------------------------------------------------


def _plain_address(match: re.Match[str]) -> str:
    # One dot leader for each dot and a modifier-letter colon: it reads the same, and Teams no longer makes it a link.
    return match.group(0).replace(".", "․").replace(":", "꞉")


def _markdown_text(text: str) -> str:
    """Card text is Markdown: keep brackets from forming links and drop anything that looks like an HTML tag. A bare
    web address or domain (Teams turns them into links) and the emphasis markers `*` and `_` are written with
    look-alike characters, so a record's name can't add a link or restyle the card."""
    text = text.replace("[", "(").replace("]", ")").replace("<", "‹").replace(">", "›")
    text = text.replace("*", "∗").replace("_", "＿")  # first: a fullwidth low line isn't a word character
    return WEB_ADDRESS.sub(_plain_address, text)


def markdown_url(url: str) -> str:
    """A URL safe inside Markdown's `(...)`: parentheses, spaces, angle brackets and non-ASCII percent-encoded."""
    return urllib.parse.quote(url, safe=":/?#@!$&'*+,;=%-._~")


def _item_block(item: Item) -> dict[str, Any]:
    title = _markdown_text(_cut(item.get("title", ""), TITLE_MAX))
    if item.get("url"):
        title = f"[{title}]({markdown_url(item['url'])})"
    blocks: list[dict[str, Any]] = [{"type": "TextBlock", "text": title, "wrap": True, "weight": "Bolder"}]
    if item.get("detail"):
        blocks.append(
            {
                "type": "TextBlock",
                "text": _markdown_text(_cut(item["detail"], DETAIL_MAX)),
                "wrap": True,
                "isSubtle": True,
                "spacing": "None",
            }
        )
    return {"type": "Container", "items": blocks, "spacing": "Medium"}


def card(heading: str, items: list[Item], intro: str = "", link: str = "", shown: int | None = None) -> dict[str, Any]:
    """An Adaptive Card 1.2 with the first `shown` items (all by default) and "and N more" for the rest."""
    shown = len(items) if shown is None else shown
    body: list[dict[str, Any]] = [
        {
            "type": "TextBlock",
            "text": _markdown_text(_cut(heading, TITLE_MAX)),
            "size": "Medium",
            "weight": "Bolder",
            "wrap": True,
        }
    ]
    if intro:
        body.append({"type": "TextBlock", "text": _markdown_text(_cut(intro, DETAIL_MAX)), "wrap": True})
    body += [_item_block(item) for item in items[:shown]]
    if len(items) > shown:
        rest = len(items) - shown
        body.append({"type": "TextBlock", "text": f"and {rest} more", "wrap": True, "isSubtle": True})
    content: dict[str, Any] = {"$schema": CARD_SCHEMA, "type": "AdaptiveCard", "version": "1.2", "body": body}
    if link:
        content["actions"] = [{"type": "Action.OpenUrl", "title": "Open Softwarden", "url": link}]
    return content


def teams_envelope(content: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "message",
        "attachments": [
            {"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None, "content": content}
        ],
    }


def teams_body(content: dict[str, Any]) -> bytes:
    """The exact request body posted to a Workflows webhook."""
    return json.dumps(teams_envelope(content), separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def card_content(heading: str, items: list[Item], intro: str = "", link: str = "") -> dict[str, Any]:
    """{"card": ...} under Teams' size limit: as many items as fit, then "and N more"."""
    full = card(heading, items, intro, link)
    if len(teams_body(full)) <= TEAMS_BUDGET:
        return {"card": full}
    low, high = 0, len(items)  # the largest count that fits (0 always does: texts are capped)
    while low < high:
        mid = (low + high + 1) // 2
        if len(teams_body(card(heading, items, intro, link, shown=mid))) <= TEAMS_BUDGET:
            low = mid
        else:
            high = mid - 1
    return {"card": card(heading, items, intro, link, shown=low)}


# ---- webhook -----------------------------------------------------------------------------------------------------


def webhook_content(event: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"event": event, "data": data}
