#!/usr/bin/env python3
"""
ingest.py, Outlook/M365-to-vault normalizer for the ingest-outlook skill.

Author:   Danny Bravo
License:  MIT
Project:  https://github.com/danilobrando/ingest-outlook

Reads a JSON payload on stdin (one of three "kind"s):
  - kind=mail      → External Inputs/Outlook/Mail/<folder|query>/<date>.md
  - kind=calendar  → External Inputs/Outlook/Calendar/<date>.md
  - kind=meetings  → External Inputs/Outlook/Meetings/<date>.md

Each kind has its own normalizer and frontmatter shape. PII guardrails:
- Mail bodies are truncated to 500 chars.
- Calendar event bodies are truncated to 400 chars.
- Meeting transcripts are NOT truncated (the whole point is to capture them).

Stdlib only. Self-contained (no _shared dependency).
"""
from __future__ import annotations

__author__ = "Danny Bravo"
__license__ = "MIT"

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

MAIL_BODY_TRUNCATE = 500
CALENDAR_BODY_TRUNCATE = 400
TRUNCATE_MARKER = "\n\n[...truncated]"

_SLUGIFY_RE = re.compile(r"[^a-z0-9]+")


# ---------------------------------------------------------------------------
# Inlined helpers (subset of skills/_shared/connector_utils.py)
# ---------------------------------------------------------------------------

def sha8(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def slugify(value: str, fallback: str = "unknown") -> str:
    s = _SLUGIFY_RE.sub("-", (value or "").lower()).strip("-")
    return s or fallback


def yaml_escape(value: Any) -> str:
    if value is None:
        return "null"
    s = str(value)
    if any(c in s for c in [':', '#', '\n', '"', "'", '[', ']', '{', '}']):
        return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return s


def parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    s = value.rstrip("Z")
    if "." in s:
        head, _, tail = s.partition(".")
        i = 0
        while i < len(tail) and tail[i].isdigit():
            i += 1
        s = head + tail[i:]
    try:
        return (
            datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
            if value.endswith("Z")
            else datetime.fromisoformat(s)
        )
    except ValueError:
        try:
            return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def to_local_str(value: str) -> str:
    dt = parse_iso(value)
    if not dt:
        return value or ""
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def to_local_sortkey(value: str) -> datetime:
    return parse_iso(value) or datetime.min.replace(tzinfo=timezone.utc)


def truncate_body(text: str, limit: int, marker: str = TRUNCATE_MARKER) -> str:
    if not text:
        return "_(body unavailable)_"
    cleaned = text.replace("```", "` ` `")
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit] + marker


def fence_text(text: str) -> str:
    if not text:
        return "_(empty)_"
    return text.replace("```", "` ` `")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def today_iso() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def date_range_strs(target_date: str, days: int) -> tuple[str, str]:
    end = target_date
    if days <= 1:
        return end, end
    start = (datetime.fromisoformat(target_date) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    return start, end


# ---------------------------------------------------------------------------
# Mail kind
# ---------------------------------------------------------------------------

def render_mail_body(payload: dict) -> tuple[str, int, list[str]]:
    messages = list(payload.get("messages") or [])
    messages.sort(key=lambda m: to_local_sortkey(m.get("internal_date") or ""))

    parts: list[str] = []
    ids: list[str] = []
    for msg in messages:
        msg_id = msg.get("id") or ""
        if msg_id:
            ids.append(msg_id)
        sender = msg.get("from") or "(unknown sender)"
        when = to_local_str(msg.get("internal_date") or "")
        subject = msg.get("subject") or "(no subject)"
        to_field = msg.get("to") or []
        cc_field = msg.get("cc") or []
        to_str = ", ".join(to_field) if isinstance(to_field, list) else str(to_field)
        cc_str = ", ".join(cc_field) if isinstance(cc_field, list) else str(cc_field)
        categories = msg.get("categories") or []
        body_text = msg.get("body_text") or ""

        parts.append(f"## {when} {sender}")
        parts.append("")
        parts.append(f"- **Subject:** {subject}")
        if to_str:
            parts.append(f"- **To:** {to_str}")
        if cc_str:
            parts.append(f"- **Cc:** {cc_str}")
        if categories:
            parts.append(f"- **Categories:** {', '.join(categories)}")
        if msg_id:
            parts.append(f"- **Outlook message ID:** `{msg_id}`")
        parts.append("")
        parts.append("### Body excerpt (max 500 chars)")
        parts.append("")
        parts.append(truncate_body(body_text, MAIL_BODY_TRUNCATE))
        parts.append("")

    body = "\n".join(parts).rstrip() + "\n"
    return body, len(messages), ids


def build_mail_frontmatter(
    payload: dict,
    count: int,
    ids: list[str],
    target_date: str,
) -> str:
    folder_or_query = payload.get("folder_or_query") or ""
    scope_kind = payload.get("scope_kind", "folder")
    days = int(payload.get("days") or 7)
    ingested_at = payload.get("ingested_at_iso") or now_iso()
    start_date, end_date = date_range_strs(target_date, days)

    lines = [
        "---",
        "type: external-input",
        "source: outlook",
        "kind: mail",
        f"folder_or_query: {yaml_escape(folder_or_query)}",
        f"scope_kind: {scope_kind}",
        f"date_range: {start_date}..{end_date}",
        f"message_count: {count}",
        f"ingested_at: {ingested_at}",
        "entity_ids:",
        "  outlook:",
    ]
    if ids:
        for mid in ids:
            lines.append(f"    - {yaml_escape(mid)}")
    else:
        lines.append("    []")
    lines.append("---")
    lines.append("")
    lines.append(f"# Outlook mail {scope_kind} {folder_or_query}, {start_date} to {end_date}")
    lines.append("")
    lines.append(
        f"_{count} message(s) ingested via /ingest-outlook mail. Bodies truncated to "
        f"{MAIL_BODY_TRUNCATE} chars to limit bulk PII._"
    )
    lines.append("")
    return "\n".join(lines)


def write_mail(payload: dict, target_date: str) -> Path:
    folder_or_query = payload.get("folder_or_query") or "unknown"
    if payload.get("scope_kind") == "query":
        scope_slug = f"query-{sha8(folder_or_query)}"
    else:
        scope_slug = slugify(folder_or_query)

    body, count, ids = render_mail_body(payload)
    fm = build_mail_frontmatter(payload, count, ids, target_date)

    vault_root = Path(payload["vault_root"])
    out_dir = vault_root / "External Inputs" / "Outlook" / "Mail" / scope_slug
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{target_date}.md"
    out_path.write_text(fm + body, encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# Calendar kind
# ---------------------------------------------------------------------------

def render_calendar_body(payload: dict) -> tuple[str, int, list[str]]:
    events = list(payload.get("events") or [])
    events.sort(key=lambda e: to_local_sortkey(e.get("start") or ""))

    parts: list[str] = []
    ids: list[str] = []
    for ev in events:
        ev_id = ev.get("id") or ""
        if ev_id:
            ids.append(ev_id)
        subject = ev.get("subject") or "(no subject)"
        start = to_local_str(ev.get("start") or "")
        end = to_local_str(ev.get("end") or "")
        organizer = ev.get("organizer") or "(unknown organizer)"
        attendees = ev.get("attendees") or []
        location = ev.get("location") or ""
        join_url = ev.get("online_meeting_url") or ""
        categories = ev.get("categories") or []
        cancelled = ev.get("is_cancelled")
        show_as = ev.get("show_as") or ""
        body_text = ev.get("body_text") or ""

        title = subject + (" (cancelled)" if cancelled else "")
        parts.append(f"## {start} {title}")
        parts.append("")
        parts.append(f"- **Start:** {start}")
        parts.append(f"- **End:** {end}")
        parts.append(f"- **Organizer:** {organizer}")
        if attendees:
            attendee_list = ", ".join(attendees[:10])
            more = f" (+{len(attendees) - 10} more)" if len(attendees) > 10 else ""
            parts.append(f"- **Attendees:** {attendee_list}{more}")
        if location:
            parts.append(f"- **Location:** {location}")
        if join_url:
            parts.append(f"- **Teams join:** {join_url}")
        if categories:
            parts.append(f"- **Categories:** {', '.join(categories)}")
        if show_as:
            parts.append(f"- **Show as:** {show_as}")
        if ev_id:
            parts.append(f"- **Event ID:** `{ev_id}`")
        parts.append("")
        parts.append(f"### Body excerpt (max {CALENDAR_BODY_TRUNCATE} chars)")
        parts.append("")
        parts.append(truncate_body(body_text, CALENDAR_BODY_TRUNCATE))
        parts.append("")

    body = "\n".join(parts).rstrip() + "\n"
    return body, len(events), ids


def build_calendar_frontmatter(
    payload: dict,
    count: int,
    ids: list[str],
    target_date: str,
) -> str:
    days = int(payload.get("days") or 7)
    ingested_at = payload.get("ingested_at_iso") or now_iso()
    start_date, end_date = date_range_strs(target_date, days)

    lines = [
        "---",
        "type: external-input",
        "source: outlook",
        "kind: calendar",
        f"date_range: {start_date}..{end_date}",
        f"event_count: {count}",
        f"ingested_at: {ingested_at}",
        "entity_ids:",
        "  outlook_event:",
    ]
    if ids:
        for eid in ids:
            lines.append(f"    - {yaml_escape(eid)}")
    else:
        lines.append("    []")
    lines.append("---")
    lines.append("")
    lines.append(f"# Outlook calendar, {start_date} to {end_date}")
    lines.append("")
    lines.append(
        f"_{count} event(s) ingested via /ingest-outlook calendar. Body excerpts "
        f"truncated to {CALENDAR_BODY_TRUNCATE} chars to limit bulk PII._"
    )
    lines.append("")
    return "\n".join(lines)


def write_calendar(payload: dict, target_date: str) -> Path:
    body, count, ids = render_calendar_body(payload)
    fm = build_calendar_frontmatter(payload, count, ids, target_date)

    vault_root = Path(payload["vault_root"])
    out_dir = vault_root / "External Inputs" / "Outlook" / "Calendar"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{target_date}.md"
    out_path.write_text(fm + body, encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# Meetings kind (Teams meetings + transcripts)
# ---------------------------------------------------------------------------

def render_meetings_body(payload: dict) -> tuple[str, int, list[str], int]:
    """Return (body, meeting_count, meeting_ids, transcript_count)."""
    meetings = list(payload.get("meetings") or [])
    meetings.sort(key=lambda m: to_local_sortkey(m.get("start") or ""))

    parts: list[str] = []
    ids: list[str] = []
    transcript_total = 0

    for mt in meetings:
        ev_id = mt.get("id") or ""
        if ev_id:
            ids.append(ev_id)
        subject = mt.get("subject") or "(no subject)"
        start = to_local_str(mt.get("start") or "")
        end = to_local_str(mt.get("end") or "")
        organizer = mt.get("organizer") or "(unknown organizer)"
        attendees = mt.get("attendees") or []
        join_url = mt.get("online_meeting_url") or ""
        body_text = mt.get("body_text") or ""
        transcripts = mt.get("transcripts") or []
        transcript_total += len(transcripts)

        parts.append(f"## {start} {subject}")
        parts.append("")
        parts.append(f"- **Start:** {start}")
        parts.append(f"- **End:** {end}")
        parts.append(f"- **Organizer:** {organizer}")
        if attendees:
            attendee_list = ", ".join(attendees[:10])
            more = f" (+{len(attendees) - 10} more)" if len(attendees) > 10 else ""
            parts.append(f"- **Attendees:** {attendee_list}{more}")
        if join_url:
            parts.append(f"- **Teams join:** {join_url}")
        if ev_id:
            parts.append(f"- **Event ID:** `{ev_id}`")
        if mt.get("meeting_id"):
            parts.append(f"- **Online meeting ID:** `{mt['meeting_id']}`")
        parts.append(f"- **Transcripts available:** {len(transcripts)}")
        parts.append("")

        if body_text.strip():
            parts.append("### Invitation excerpt")
            parts.append("")
            parts.append(truncate_body(body_text, CALENDAR_BODY_TRUNCATE))
            parts.append("")

        for i, t in enumerate(transcripts, 1):
            tid = t.get("id") or ""
            t_when = to_local_str(t.get("created_at") or "")
            content = t.get("content_vtt") or ""
            parts.append(f"### Transcript {i}{f' ({t_when})' if t_when else ''}")
            parts.append("")
            if tid:
                parts.append(f"- **Transcript ID:** `{tid}`")
                parts.append("")
            if content.strip():
                parts.append("```vtt")
                parts.append(fence_text(content))
                parts.append("```")
            else:
                parts.append("_(transcript content unavailable)_")
            parts.append("")

    body = "\n".join(parts).rstrip() + "\n"
    return body, len(meetings), ids, transcript_total


def build_meetings_frontmatter(
    payload: dict,
    count: int,
    ids: list[str],
    transcript_count: int,
    target_date: str,
) -> str:
    days = int(payload.get("days") or 7)
    ingested_at = payload.get("ingested_at_iso") or now_iso()
    start_date, end_date = date_range_strs(target_date, days)

    lines = [
        "---",
        "type: external-input",
        "source: outlook",
        "kind: meetings",
        f"date_range: {start_date}..{end_date}",
        f"meeting_count: {count}",
        f"transcript_count: {transcript_count}",
        f"ingested_at: {ingested_at}",
        "entity_ids:",
        "  outlook_event:",
    ]
    if ids:
        for eid in ids:
            lines.append(f"    - {yaml_escape(eid)}")
    else:
        lines.append("    []")
    lines.append("---")
    lines.append("")
    lines.append(f"# Teams meetings + transcripts, {start_date} to {end_date}")
    lines.append("")
    lines.append(
        f"_{count} meeting(s), {transcript_count} transcript(s) ingested via "
        f"/ingest-outlook meetings. Transcripts are stored verbatim in VTT format._"
    )
    lines.append("")
    return "\n".join(lines)


def write_meetings(payload: dict, target_date: str) -> Path:
    body, count, ids, transcript_count = render_meetings_body(payload)
    fm = build_meetings_frontmatter(payload, count, ids, transcript_count, target_date)

    vault_root = Path(payload["vault_root"])
    out_dir = vault_root / "External Inputs" / "Outlook" / "Meetings"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{target_date}.md"
    out_path.write_text(fm + body, encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Outlook/M365-to-vault normalizer. Reads a JSON payload on stdin "
            "(kind=mail|calendar|meetings) and writes a dated vault file."
        ),
    )
    parser.add_argument(
        "--target-date",
        help="Override the target date (YYYY-MM-DD). Defaults to today in local time.",
    )
    args = parser.parse_args()

    try:
        raw = sys.stdin.read()
    except Exception as e:
        print(f"ERROR: failed to read stdin: {e}", file=sys.stderr)
        return 2

    if not raw.strip():
        print("ERROR: empty stdin. Pipe a JSON payload from fetch.py.", file=sys.stderr)
        return 2

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: invalid JSON on stdin: {e}", file=sys.stderr)
        return 2

    if "vault_root" not in payload:
        print("ERROR: missing required field: vault_root", file=sys.stderr)
        return 2

    kind = payload.get("kind") or "mail"  # back-compat: legacy mail payloads omitted kind
    target_date = args.target_date or payload.get("target_date") or today_iso()

    if kind == "mail":
        if "folder_or_query" not in payload:
            print("ERROR: mail payload missing folder_or_query", file=sys.stderr)
            return 2
        out_path = write_mail(payload, target_date)
        count = len(payload.get("messages") or [])
        print(f"Wrote {count} message(s) to {out_path}")
    elif kind == "calendar":
        out_path = write_calendar(payload, target_date)
        count = len(payload.get("events") or [])
        print(f"Wrote {count} event(s) to {out_path}")
    elif kind == "meetings":
        out_path = write_meetings(payload, target_date)
        count = len(payload.get("meetings") or [])
        transcripts = sum(len(m.get("transcripts") or []) for m in payload.get("meetings") or [])
        print(f"Wrote {count} meeting(s), {transcripts} transcript(s) to {out_path}")
    else:
        print(f"ERROR: unknown payload kind: {kind!r}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
