"""
sync_cerebro.py, the "cerebro" layout writer used by `fetch.py sync`.

Author:   Danny Bravo
License:  MIT
Project:  https://github.com/danilobrando/ingest-outlook

Writes the raw layer of a System 3 v0.2 vault (contract sections 2, 3 and 7):

  <raw_root>/correo/<empresa>/AAAA-MM-DD/<HHMM>-<asunto-slug>.md   immutable
  <raw_root>/calendario/<empresa>/AAAA-MM-DD.md                     rewritten per run
  <raw_root>/reuniones/<empresa>/AAAA-MM-DD-<reunion-slug>.md (+ .vtt)  immutable

This module never talks to the network directly: `fetch.py` passes a small
Graph adapter (get_json / get_text) that already handles tokens, 429 and
mid-run 401 refresh. It never calls a Graph write endpoint and never writes
the `sintetico` key.

Stdlib only.
"""
from __future__ import annotations

__author__ = "Danny Bravo"
__version__ = "0.6.0"
__license__ = "MIT"

import hashlib
import html
import json
import os
import re
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

MAIL_BODY_MAX_BYTES = 100 * 1024
MAIL_TRUNCATED_MARKER = "[...truncado a 100 KB]"
CALENDAR_EXCERPT_CHARS = 400
PAGE_SIZE = 50
CALENDAR_MAX_PAGES = 40
MAX_PATH_CHARS = 240
MAX_SLUG_CHARS = 60
MAX_CONSECUTIVE_WRITE_ERRORS = 5
PREFER_TEXT = 'outlook.body-content-type="text"'

MAIL_SELECT = ",".join([
    "id", "conversationId", "parentFolderId", "subject", "from", "sender",
    "toRecipients", "ccRecipients", "receivedDateTime", "hasAttachments", "body",
])
CALENDAR_SELECT = ",".join([
    "id", "subject", "body", "bodyPreview", "organizer", "attendees", "start", "end",
    "location", "isAllDay", "isCancelled", "isOnlineMeeting", "onlineMeeting",
    "onlineMeetingProvider",
])

INDEX_MAIL = "ids-correo.txt"
INDEX_MEETINGS = "ids-reuniones.txt"
STATE_FILE = "state.json"
ACCOUNT_FILE = "cuenta.json"

_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


class NeedsLogin(Exception):
    """The profile has no usable refresh token. Only `fix --profile X` (a
    person at the keyboard) can repair it; automation must never open a
    browser."""


class GraphError(Exception):
    def __init__(self, status: int, code: str = "", message: str = "", inner_code: str = ""):
        super().__init__(f"HTTP {status} {inner_code or code}".strip())
        self.status = status
        self.code = code or ""
        self.message = message or ""
        self.inner_code = inner_code or ""

    def has(self, token: str) -> bool:
        return token in (self.inner_code, self.code) or token in self.message

    def motive(self) -> str:
        """Short, non-personal reason for the vault log."""
        for token in ("GraphAccessToTranscriptsDisabled", "SpeakerAttributionNotAllowed"):
            if self.has(token):
                return f"{self.status} {token}"
        return f"{self.status} {self.inner_code or self.code}".strip()


@dataclass
class ProfileSettings:
    slug: str
    empresa: str
    raw_root: str
    backfill_days: int = 30
    calendar_past_days: int = 1
    calendar_ahead_days: int = 14
    mail_exclude_folders: list[str] = field(default_factory=lambda: ["junkemail", "deleteditems", "drafts"])
    max_messages_per_run: int = 1000
    teams: bool = False


@dataclass
class RunResult:
    perfil: str
    correo_nuevos: int = 0
    calendario_dias: int = 0
    reuniones: int = 0
    reuniones_nota: str = ""
    estado: str = "ok"
    notas: list[str] = field(default_factory=list)
    exit_code: int = 0
    planned: list[str] = field(default_factory=list)

    def log_line(self, when_iso: str) -> str:
        reuniones = f"reuniones {self.reuniones}"
        if self.reuniones_nota:
            reuniones += f" ({self.reuniones_nota})"
        estado = f"estado {self.estado}"
        if self.notas:
            estado += "; " + "; ".join(self.notas)
        return " | ".join([
            when_iso,
            self.perfil or "-",
            f"correo {self.correo_nuevos} nuevos",
            f"calendario {self.calendario_dias} días",
            reuniones,
            estado,
        ])


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def sanitize_text(value: Any) -> str:
    """Drop lone surrogates (they cannot be written as UTF-8)."""
    text = "" if value is None else str(value)
    return text.encode("utf-8", errors="replace").decode("utf-8")


def slugify(value: Any, fallback: str = "sin-asunto", max_chars: int = MAX_SLUG_CHARS) -> str:
    text = unicodedata.normalize("NFKD", sanitize_text(value))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    if len(text) > max_chars:
        cut = text[:max_chars]
        if "-" in cut[max_chars // 2:]:
            cut = cut[: cut.rfind("-")]
        text = cut.strip("-")
    text = text or fallback
    if text in _WINDOWS_RESERVED:
        text = f"item-{text}"
    return text


def sha6(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:6]


_YAML_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def yaml_quote(value: Any) -> str:
    """Double-quoted YAML scalar that is also a valid JSON string.

    Escapes quotes, backslashes, control characters, C1 controls, NEL,
    U+2028/U+2029 and BOM, so no subject can break the frontmatter.
    """
    out = ['"']
    for ch in sanitize_text(value):
        code = ord(ch)
        if ch in _YAML_ESCAPES:
            out.append(_YAML_ESCAPES[ch])
        elif code < 0x20 or 0x7F <= code <= 0x9F or code in (0x2028, 0x2029, 0xFEFF, 0xFFFE, 0xFFFF):
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def yaml_list(key: str, items: Iterable[str]) -> list[str]:
    values = [v for v in items if v]
    if not values:
        return [f"{key}: []"]
    return [f"{key}:"] + [f"  - {yaml_quote(v)}" for v in values]


def one_line(value: Any) -> str:
    return re.sub(r"\s+", " ", sanitize_text(value)).strip()


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def neutralize_rules(text: str) -> str:
    """A body line that is exactly `---` could be read as a frontmatter fence."""
    return "\n".join("\\---" if line.strip() == "---" else line for line in text.split("\n"))


def truncate_utf8(text: str, max_bytes: int) -> tuple[str, bool]:
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text, False
    return data[:max_bytes].decode("utf-8", errors="ignore"), True


class _HTMLToText(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "blockquote"}
    SKIP = {"style", "script", "head", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
            if tag == "li":
                self.parts.append("- ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _HTMLToText()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # malformed HTML: keep a tag-stripped fallback
        return html.unescape(re.sub(r"<[^>]+>", " ", markup))
    text = "".join(parser.parts)
    text = re.sub(r"[ \t\xa0]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def body_text(item: dict[str, Any]) -> str:
    body = item.get("body") or {}
    content = sanitize_text(body.get("content") or "")
    if str(body.get("contentType") or "").lower() == "html":
        content = html_to_text(content)
    if not content.strip():
        content = sanitize_text(item.get("bodyPreview") or "")
    return normalize_newlines(content)


_NAME_EDGE_CHARS = "\"'`\u201c\u201d\u2018\u2019\u00ab\u00bb "


def normalize_email(value: Any) -> str:
    text = one_line(value).strip().strip("<>").strip()
    return text.lower()


def normalize_name(value: Any) -> str:
    text = one_line(value).replace("<", "").replace(">", "")
    previous = None
    while previous != text:
        previous = text
        text = text.strip().strip(_NAME_EDGE_CHARS).strip()
    return re.sub(r"\s+", " ", text)


def person(entry: dict[str, Any] | None) -> str:
    """`Nombre <correo>`, or `<correo>` when there is no usable name.

    Email trimmed and lowercased; name trimmed, whitespace collapsed,
    surrounding quotes removed, no angle brackets. No address -> "" (skipped).
    """
    address = (entry or {}).get("emailAddress") or {}
    email = normalize_email(address.get("address"))
    if not email:
        return ""
    name = normalize_name(address.get("name"))
    if not name or name.lower() == email:
        return f"<{email}>"
    return f"{name} <{email}>"


def _person_key(text: str) -> str:
    match = re.search(r"<([^>]*)>\s*$", text)
    return (match.group(1) if match else text).strip().lower()


def unique_people(people: Iterable[str]) -> list[str]:
    """Deduplicate by email (case-insensitive), keeping the first occurrence."""
    seen: set[str] = set()
    out: list[str] = []
    for text in people:
        if not text:
            continue
        key = _person_key(text)
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def is_resource(attendee: dict[str, Any]) -> bool:
    return str((attendee or {}).get("type") or "").lower() == "resource"


def event_people(ev: dict[str, Any]) -> list[str]:
    """Organizer first, then attendees; rooms (type resource) excluded."""
    return unique_people(
        [person(ev.get("organizer"))]
        + [person(a) for a in ev.get("attendees") or [] if not is_resource(a)]
    )


def event_place(ev: dict[str, Any]) -> str:
    place = one_line((ev.get("location") or {}).get("displayName"))
    parts = [place] if place else []
    for attendee in ev.get("attendees") or []:
        if not is_resource(attendee):
            continue
        address = attendee.get("emailAddress") or {}
        room = normalize_name(address.get("name")) or normalize_email(address.get("address"))
        if room and all(room.lower() not in part.lower() for part in parts):
            parts.append(room)
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def parse_graph_datetime(value: Any, zone: Any = "UTC") -> datetime | None:
    """Parse Graph timestamps (7-digit fractions, optional Z, often naive UTC)."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    head, dot, tail = text.partition(".")
    if dot:
        i = 0
        while i < len(tail) and tail[i].isdigit():
            i += 1
        text = head + ("." + tail[:6] if i else "") + tail[i:]
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # calendarView and messages come back in UTC unless a Prefer
        # outlook.timezone header says otherwise; we never send one.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_iso(dt: datetime) -> str:
    return dt.astimezone().replace(microsecond=0).isoformat(timespec="seconds")


def local_midnight(day: date) -> datetime:
    return datetime.combine(day, datetime_time.min).astimezone()


# ---------------------------------------------------------------------------
# Files: atomic writes, indices, state
# ---------------------------------------------------------------------------

def atomic_write_text(path: Path, text: str, newline: str | None = "\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8", newline=newline) as handle:
        handle.write(text)
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            pass
    last: Exception | None = None
    for attempt in range(8):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:  # Windows: antivirus/indexer holds the target
            last = exc
            time.sleep(0.1 * (attempt + 1))
    try:
        tmp.unlink()
    except OSError:
        pass
    raise last if last else OSError(f"cannot replace {path}")


def load_index(path: Path) -> set[str]:
    try:
        with open(path, encoding="utf-8") as handle:
            return {line.strip() for line in handle if line.strip()}
    except FileNotFoundError:
        return set()


def append_index(path: Path, item_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(item_id + "\n")
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            pass


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_json(path: Path, data: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def read_frontmatter_value(path: Path, key: str) -> str | None:
    """Read one scalar from an existing raw file's frontmatter."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            first = handle.readline()
            if first.strip() != "---":
                return None
            for _ in range(200):
                line = handle.readline()
                if not line or line.strip() == "---":
                    return None
                if line.startswith(f"{key}:"):
                    raw = line[len(key) + 1:].strip()
                    if raw.startswith('"'):
                        try:
                            return json.loads(raw)
                        except ValueError:
                            return raw.strip('"')
                    return raw
    except OSError:
        return None
    return None


def rel_posix(vault: Path, path: Path) -> str:
    try:
        return path.relative_to(vault).as_posix()
    except ValueError:
        return path.as_posix()


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def odata_url(base: str, params: dict[str, Any]) -> str:
    parts = [f"{key}={urllib.parse.quote(str(value), safe='/,:')}" for key, value in params.items()]
    return f"{base}?{'&'.join(parts)}"


EMPRESA_RE = re.compile(r"^[a-z0-9-]{1,32}$")


def validate_empresa(value: Any) -> str:
    """`empresa` is what every downstream note inherits: never empty."""
    text = str(value or "").strip()
    if not EMPRESA_RE.fullmatch(text) or text in _WINDOWS_RESERVED:
        raise ValueError("empresa must be a slug [a-z0-9-]{1,32} (for example acme)")
    return text


class ProfileRun:
    def __init__(
        self,
        graph: Any,
        settings: ProfileSettings,
        profile_dir: Path,
        vault: Path,
        *,
        run_at: datetime,
        deadline: float,
        dry_run: bool = False,
        only: set[str] | None = None,
        out: Callable[[str], None] = print,
        verbose: Callable[[str], None] | None = None,
    ):
        self.graph = graph
        settings.empresa = validate_empresa(settings.empresa)
        self.s = settings
        self.profile_dir = Path(profile_dir)
        self.vault = Path(vault)
        self.run_at = run_at.astimezone()
        self.ingestado = local_iso(self.run_at)
        self.deadline = deadline
        self.dry_run = dry_run
        self.only = only or {"mail", "calendar", "meetings"}
        self.out = out
        self.verbose = verbose or (lambda _msg: None)
        self.result = RunResult(perfil=settings.slug)
        self.raw_root = self.vault / Path(*PurePosixPath(settings.raw_root).parts)
        self.planned_paths: set[str] = set()
        self.cuenta = ""
        self.folder_cache: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] | None = None

    # -- helpers ------------------------------------------------------------

    def time_left(self) -> float:
        return self.deadline - time.monotonic()

    def plan(self, path: Path, what: str) -> None:
        rel = rel_posix(self.vault, path)
        self.result.planned.append(rel)
        self.out(f"[dry-run] {what}: {rel}")

    def _fit_path(self, directory: Path, stem: str, suffix: str) -> str:
        full = str((directory / f"{stem}{suffix}").resolve()) if directory.is_absolute() else str(directory / f"{stem}{suffix}")
        if len(full) <= MAX_PATH_CHARS:
            return stem
        excess = len(full) - MAX_PATH_CHARS
        keep = max(12, len(stem) - excess - 7)
        return f"{stem[:keep].rstrip('-')}-{sha6(stem)}"

    def choose_path(self, directory: Path, stem: str, suffix: str, item_id: str) -> tuple[Path, bool]:
        """First free name for an immutable item. Returns (path, already_written).

        Same name taken by another id -> `-<sha6 of id>` suffix. A file with our
        own id means an earlier run wrote it but stopped before the index.
        """
        stem = self._fit_path(directory, stem, suffix)
        candidates = [stem, f"{stem}-{sha6(item_id)}"]
        candidates += [f"{stem}-{sha6(item_id)}-{n}" for n in range(2, 50)]
        for candidate in candidates:
            path = directory / f"{candidate}{suffix}"
            key = str(path).lower()
            if key in self.planned_paths:
                continue
            if path.exists():
                if read_frontmatter_value(path, "id_origen") == item_id:
                    return path, True
                continue
            self.planned_paths.add(key)
            return path, False
        raise OSError(f"no free file name for {stem}{suffix}")

    def load_account(self) -> str:
        cached = load_json(self.profile_dir / ACCOUNT_FILE).get("cuenta")
        if cached:
            return str(cached)
        me = self.graph.get_json(odata_url(f"{self.graph.base}/me", {"$select": "userPrincipalName,mail"}))
        cuenta = str(me.get("userPrincipalName") or me.get("mail") or "")
        if cuenta and not self.dry_run:
            save_json(self.profile_dir / ACCOUNT_FILE, {"cuenta": cuenta, "desde": self.ingestado})
        return cuenta

    # -- run ------------------------------------------------------------------

    def _stage(self, name: str, action: Callable[[], None]) -> bool:
        """Run one source; a Graph error in it is recorded and the other
        sources still run (a calendar 403 must not cost the mail)."""
        try:
            action()
            return True
        except GraphError as exc:
            message = f"error: {name} Graph {exc.motive()}"
            if self.result.estado == "ok":
                self.result.estado = message
            else:
                self.result.notas.append(message)
            self.result.exit_code = max(self.result.exit_code, 1)
            return False

    def run(self) -> RunResult:
        self.cuenta = self.load_account()
        events_ok = True
        if "calendar" in self.only or ("meetings" in self.only and self.s.teams):
            events_ok = self._stage("calendario", self.fetch_events)
        if "calendar" in self.only and events_ok:
            self._stage("calendario", self.write_calendar)
        if "meetings" in self.only and self.s.teams and events_ok:
            if self.time_left() > 0:
                self._stage("reuniones", self.sync_meetings)
            else:
                self.result.reuniones_nota = "pendiente, continúa en la siguiente"
        if "mail" in self.only:
            self._stage("correo", self.sync_mail)
        return self.result

    # -- mail -----------------------------------------------------------------

    def resolve_excluded_folders(self) -> set[str]:
        excluded: set[str] = set()
        for name in self.s.mail_exclude_folders:
            name = name.strip()
            if not name:
                continue
            try:
                data = self.graph.get_json(odata_url(
                    f"{self.graph.base}/me/mailFolders/{urllib.parse.quote(name, safe='')}",
                    {"$select": "id,displayName"},
                ))
            except GraphError as exc:
                if exc.status in (400, 404):
                    self.verbose(f"carpeta excluida {name!r} no existe en este buzón")
                    continue
                raise
            if data.get("id"):
                excluded.add(str(data["id"]))
        return excluded

    def folder_info(self, folder_id: str) -> dict[str, Any]:
        if folder_id in self.folder_cache:
            return self.folder_cache[folder_id]
        info: dict[str, Any] = {}
        try:
            info = self.graph.get_json(odata_url(
                f"{self.graph.base}/me/mailFolders/{urllib.parse.quote(folder_id, safe='')}",
                {"$select": "id,displayName,parentFolderId"},
            ))
        except GraphError as exc:
            if exc.status not in (400, 403, 404):
                raise
        self.folder_cache[folder_id] = info
        return info

    def is_excluded(self, folder_id: str, excluded: set[str]) -> bool:
        """True when the folder or any ancestor is excluded (a subfolder of
        Deleted Items counts as deleted)."""
        current = folder_id
        for _ in range(10):
            if not current:
                return False
            if current in excluded:
                return True
            parent = str(self.folder_info(current).get("parentFolderId") or "")
            if not parent or parent == current:
                return False
            current = parent
        return False

    def attachments(self, message_id: str) -> list[dict[str, Any]]:
        url = odata_url(
            f"{self.graph.base}/me/messages/{urllib.parse.quote(message_id, safe='')}/attachments",
            {"$select": "name,contentType,size"},
        )
        items: list[dict[str, Any]] = []
        pages = 0
        while url and pages < 5:
            data = self.graph.get_json(url)
            items.extend(data.get("value") or [])
            url = data.get("@odata.nextLink")
            pages += 1
        return items

    def empresa(self) -> str:
        """Single source for both the `<empresa>` folder and the frontmatter."""
        value = self.s.empresa
        if not value or not EMPRESA_RE.fullmatch(value):
            raise ValueError("empresa vacía o inválida: no se escribe ningún crudo sin empresa")
        return value

    def render_mail(self, msg: dict[str, Any], received: datetime, attachments: list[dict[str, Any]], carpeta: str) -> str:
        sender = person(msg.get("from") or msg.get("sender"))
        people = unique_people(
            [sender]
            + [person(p) for p in msg.get("toRecipients") or []]
            + [person(p) for p in msg.get("ccRecipients") or []]
        )
        names = [one_line(a.get("name")) or "(sin nombre)" for a in attachments]
        lines = [
            "---",
            "fuente: correo",
            f"empresa: {self.empresa()}",
            f"cuenta: {yaml_quote(self.cuenta)}",
            f"fecha: {local_iso(received)}",
            f"id_origen: {yaml_quote(msg.get('id') or '')}",
            *yaml_list("participantes", people),
            f"asunto: {yaml_quote(msg.get('subject') or '')}",
            "proyecto:",
            f"carpeta: {yaml_quote(carpeta)}",
            f"hilo: {yaml_quote(msg.get('conversationId') or '')}",
            *yaml_list("adjuntos", names),
            f"ingestado: {self.ingestado}",
            "---",
            "",
        ]
        if attachments:
            lines.append("## Adjuntos")
            lines.append("")
            for item, name in zip(attachments, names):
                kind = one_line(item.get("contentType")) or "tipo desconocido"
                size = item.get("size")
                size_text = f"{max(1, round(int(size) / 1024))} KB" if isinstance(size, (int, float)) else "tamaño desconocido"
                lines.append(f"- {name} ({kind}, {size_text})")
            lines.append("")
        text = neutralize_rules(body_text(msg).strip("\n"))
        text, truncated = truncate_utf8(text, MAIL_BODY_MAX_BYTES)
        lines.append("## Cuerpo")
        lines.append("")
        lines.append(text.rstrip())
        if truncated:
            lines.append("")
            lines.append(MAIL_TRUNCATED_MARKER)
        return "\n".join(lines).rstrip("\n") + "\n"

    def write_message(self, msg: dict[str, Any], index: set[str], index_path: Path, excluded: set[str]) -> str:
        """Returns 'written', 'known', 'excluded' or 'skipped'."""
        message_id = str(msg.get("id") or "")
        if not message_id:
            return "skipped"
        if message_id in index:
            return "known"
        folder_id = str(msg.get("parentFolderId") or "")
        if folder_id and self.is_excluded(folder_id, excluded):
            return "excluded"
        received = parse_graph_datetime(msg.get("receivedDateTime"))
        if received is None:
            return "skipped"
        local = received.astimezone()
        directory = self.raw_root / "correo" / self.empresa() / local.strftime("%Y-%m-%d")
        stem = f"{local.strftime('%H%M')}-{slugify(msg.get('subject'))}"
        path, already = self.choose_path(directory, stem, ".md", message_id)
        if already:
            if not self.dry_run:
                append_index(index_path, message_id)
            index.add(message_id)
            return "known"
        attachments = self.attachments(message_id) if msg.get("hasAttachments") else []
        carpeta = one_line(self.folder_info(folder_id).get("displayName")) if folder_id else ""
        content = self.render_mail(msg, received, attachments, carpeta)
        if self.dry_run:
            self.plan(path, "correo")
        else:
            atomic_write_text(path, content)
            append_index(index_path, message_id)
        index.add(message_id)
        return "written"

    def sync_mail(self) -> None:
        """Whole mailbox (minus excluded folders) by receivedDateTime watermark.

        The watermark only advances through the contiguous prefix of messages
        handled successfully, and is saved even when the run stops early
        (time budget, cap, Graph error), so the next run continues from there.
        """
        state_path = self.profile_dir / STATE_FILE
        state = load_json(state_path)
        mail_state = state.get("correo") if isinstance(state.get("correo"), dict) else {}
        mark = str(mail_state.get("marca") or "")
        if not mark:
            mark = iso_z(datetime.now(timezone.utc) - timedelta(days=self.s.backfill_days))
        index_path = self.profile_dir / INDEX_MAIL
        index = load_index(index_path)
        limit = max(1, int(self.s.max_messages_per_run))
        processed = 0
        new_mark = mark
        frozen = False
        consecutive_errors = 0
        errors = 0
        stop: str | None = None
        try:
            excluded = self.resolve_excluded_folders()
            url: str | None = odata_url(f"{self.graph.base}/me/messages", {
                "$filter": f"receivedDateTime ge {mark}",
                "$orderby": "receivedDateTime asc",
                "$select": MAIL_SELECT,
                "$top": str(min(PAGE_SIZE, limit)),
            })
            while url and not stop:
                page = self.graph.get_json(url, prefer=PREFER_TEXT)
                for msg in page.get("value") or []:
                    if processed >= limit:
                        stop = "tope"
                        break
                    processed += 1
                    try:
                        outcome = self.write_message(msg, index, index_path, excluded)
                        consecutive_errors = 0
                    except OSError as exc:
                        errors += 1
                        consecutive_errors += 1
                        frozen = True
                        self.verbose(f"no se pudo escribir un correo: {type(exc).__name__}: {exc}")
                        if consecutive_errors >= MAX_CONSECUTIVE_WRITE_ERRORS:
                            stop = "errores"
                            break
                        continue
                    if outcome == "written":
                        self.result.correo_nuevos += 1
                    if not frozen and msg.get("receivedDateTime"):
                        new_mark = str(msg["receivedDateTime"])
                    if self.time_left() <= 0:
                        stop = "tiempo"
                        break
                if stop:
                    break
                url = page.get("@odata.nextLink")
                if url and processed >= limit:
                    stop = "tope"
        finally:
            if not self.dry_run and new_mark != mark:
                state = load_json(state_path)
                correo = state.get("correo") if isinstance(state.get("correo"), dict) else {}
                correo["marca"] = new_mark
                correo["actualizado"] = self.ingestado
                state["correo"] = correo
                state["version"] = 1
                save_json(state_path, state)
        if stop == "tope":
            self.result.notas.append(f"tope de {limit} mensajes alcanzado, continúa en la siguiente")
        elif stop == "tiempo":
            self.result.notas.append("tope de tiempo alcanzado, continúa en la siguiente")
        if errors:
            self.result.estado = f"error: {errors} correos sin escribir"
            self.result.exit_code = max(self.result.exit_code, 1)

    # -- calendar -------------------------------------------------------------

    def window_days(self) -> list[date]:
        today = self.run_at.date()
        start = today - timedelta(days=self.s.calendar_past_days)
        end = today + timedelta(days=self.s.calendar_ahead_days)
        return [start + timedelta(days=n) for n in range((end - start).days + 1)]

    def fetch_events(self) -> None:
        days = self.window_days()
        start = local_midnight(days[0])
        end = local_midnight(days[-1] + timedelta(days=1))
        url: str | None = odata_url(f"{self.graph.base}/me/calendarView", {
            "startDateTime": iso_z(start),
            "endDateTime": iso_z(end),
            "$top": str(PAGE_SIZE),
            "$orderby": "start/dateTime",
            "$select": CALENDAR_SELECT,
        })
        events: list[dict[str, Any]] = []
        pages = 0
        while url and pages < CALENDAR_MAX_PAGES:
            data = self.graph.get_json(url, prefer=PREFER_TEXT)
            events.extend(data.get("value") or [])
            url = data.get("@odata.nextLink")
            pages += 1
        if url:
            self.result.notas.append(f"calendario con más de {CALENDAR_MAX_PAGES} páginas, incompleto")
        self.events = events

    @staticmethod
    def event_bounds(ev: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
        start = ev.get("start") or {}
        end = ev.get("end") or {}
        return (
            parse_graph_datetime(start.get("dateTime"), start.get("timeZone")),
            parse_graph_datetime(end.get("dateTime"), end.get("timeZone")),
        )

    def event_days(self, ev: dict[str, Any]) -> list[date]:
        start, end = self.event_bounds(ev)
        if start is None:
            return []
        if ev.get("isAllDay"):
            # All-day dates are floating: take the calendar date literally.
            first = start.date()
            last = (end.date() - timedelta(days=1)) if end is not None and end.date() > first else first
        else:
            local_start = start.astimezone()
            first = local_start.date()
            if end is not None and end.astimezone() > local_start:
                last = (end.astimezone() - timedelta(microseconds=1)).date()
            else:
                last = first
        if (last - first).days > 60:
            last = first + timedelta(days=60)
        return [first + timedelta(days=n) for n in range((last - first).days + 1)]

    @staticmethod
    def is_teams(ev: dict[str, Any]) -> bool:
        join = ((ev.get("onlineMeeting") or {}).get("joinUrl") or "")
        provider = str(ev.get("onlineMeetingProvider") or "").lower()
        return bool(join) and (bool(ev.get("isOnlineMeeting")) or provider == "teamsforbusiness" or "teams.microsoft" in join)

    def render_event_block(self, ev: dict[str, Any]) -> list[str]:
        subject = one_line(ev.get("subject")) or "(sin asunto)"
        if ev.get("isAllDay"):
            header = f"## Todo el día · {subject}"
        else:
            start, end = self.event_bounds(ev)
            start_text = start.astimezone().strftime("%H:%M") if start else "??:??"
            end_text = end.astimezone().strftime("%H:%M") if end else "??:??"
            header = f"## {start_text}–{end_text} · {subject}"
        lines = [
            header,
            f"- id_origen: {one_line(ev.get('id'))}",
            f"- organizador: {person(ev.get('organizer'))}",
            f"- participantes: {'; '.join(event_people(ev))}",
            f"- lugar: {event_place(ev)}",
            f"- teams: {'sí' if self.is_teams(ev) else 'no'}",
        ]
        if ev.get("isCancelled"):
            lines.append("- estado: cancelado")
        excerpt = one_line(body_text(ev))[:CALENDAR_EXCERPT_CHARS].rstrip()
        if excerpt:
            lines.append(f"- resumen: {excerpt}")
        return [line.rstrip() for line in lines]

    def render_day(self, day: date, events: list[dict[str, Any]]) -> str:
        def sort_key(ev: dict[str, Any]) -> tuple[int, str, str]:
            start, _ = self.event_bounds(ev)
            return (0 if ev.get("isAllDay") else 1, start.isoformat() if start else "", str(ev.get("id") or ""))

        ordered = sorted(events, key=sort_key)
        people: list[str] = []
        for ev in ordered:
            people.extend(event_people(ev))
        lines = [
            "---",
            "fuente: calendario",
            f"empresa: {self.empresa()}",
            f"cuenta: {yaml_quote(self.cuenta)}",
            f"fecha: {local_iso(local_midnight(day))}",
            *yaml_list("id_origen", [str(ev.get("id") or "") for ev in ordered]),
            *yaml_list("participantes", unique_people(people)),
            f"asunto: {yaml_quote('Agenda ' + day.isoformat())}",
            "proyecto:",
            f"eventos: {len(ordered)}",
            f"ingestado: {self.ingestado}",
            "---",
            "",
        ]
        if not ordered:
            lines.append("Sin eventos.")
        for i, ev in enumerate(ordered):
            if i:
                lines.append("")
            lines.extend(self.render_event_block(ev))
        return "\n".join(lines).rstrip("\n") + "\n"

    @staticmethod
    def _without_ingestado(text: str) -> str:
        return re.sub(r"(?m)^ingestado: .*$", "", text)

    def write_calendar(self) -> None:
        days = self.window_days()
        by_day: dict[date, list[dict[str, Any]]] = {day: [] for day in days}
        for ev in self.events or []:
            for day in self.event_days(ev):
                if day in by_day:
                    by_day[day].append(ev)
        directory = self.raw_root / "calendario" / self.empresa()
        for day in days:
            path = directory / f"{day.isoformat()}.md"
            content = self.render_day(day, by_day[day])
            self.result.calendario_dias += 1
            if self.dry_run:
                self.plan(path, "calendario")
                continue
            try:
                existing = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                existing = None
            if existing is not None and self._without_ingestado(existing) == self._without_ingestado(content):
                continue  # unchanged: keep the bytes so hash-based consumers see no novelty
            atomic_write_text(path, content)

    # -- meetings -------------------------------------------------------------

    def sync_meetings(self) -> None:
        """Teams transcripts, following docs/teams-transcripts-research.md:
        JoinWebUrl -> onlineMeeting id -> transcripts -> content (text/vtt,
        falling back to the unattributed format on SpeakerAttributionNotAllowed).
        A 403 is logged with its code and never stops mail or calendar."""
        now = datetime.now(timezone.utc)
        window_start = local_midnight(self.window_days()[0])
        cutoff = now - timedelta(days=max(self.s.backfill_days, self.s.calendar_past_days + 1))
        candidates = []
        for ev in self.events or []:
            if not self.is_teams(ev) or ev.get("isCancelled"):
                continue
            start, end = self.event_bounds(ev)
            if start is None or start < window_start or (end or start) > now:
                continue
            candidates.append(ev)
        index_path = self.profile_dir / INDEX_MEETINGS
        index = load_index(index_path)
        forbidden: str | None = None
        not_found = 0
        seen_meetings: set[str] = set()
        for ev in candidates:
            if self.time_left() <= 0:
                self.result.notas.append("reuniones pendientes, continúa en la siguiente")
                break
            join = (ev.get("onlineMeeting") or {}).get("joinUrl") or ""
            try:
                meeting_id = self.resolve_meeting(join)
                if not meeting_id:
                    not_found += 1  # guest without access, expired, or not a real Teams meeting
                    continue
                if meeting_id in seen_meetings:
                    continue
                seen_meetings.add(meeting_id)
                transcripts = self.graph.get_json(
                    f"{self.graph.base}/me/onlineMeetings/{urllib.parse.quote(meeting_id, safe='')}/transcripts"
                ).get("value") or []
            except GraphError as exc:
                if exc.status == 403:
                    forbidden = forbidden or exc.motive()
                    if exc.has("GraphAccessToTranscriptsDisabled"):
                        break  # tenant-wide switch: every other meeting fails the same way
                    continue
                if exc.status == 404:
                    continue
                raise
            for transcript in transcripts:
                tid = str(transcript.get("id") or "")
                if not tid or tid in index:
                    continue
                created = parse_graph_datetime(transcript.get("createdDateTime"))
                if created is not None and created < cutoff:
                    continue
                try:
                    vtt, attributed = self.transcript_content(meeting_id, tid)
                except GraphError as exc:
                    if exc.status == 403:
                        forbidden = forbidden or exc.motive()
                        continue
                    if exc.status == 404:
                        continue
                    raise
                if self.write_meeting(ev, transcript, vtt, index, index_path, attributed):
                    self.result.reuniones += 1
        notes = []
        if forbidden:
            notes.append(f"sin acceso: {forbidden}")
        if not_found:
            notes.append(f"{not_found} sin datos de Teams")
        self.result.reuniones_nota = "; ".join(notes)

    def transcript_content(self, meeting_id: str, transcript_id: str) -> tuple[str, bool]:
        base = (
            f"{self.graph.base}/me/onlineMeetings/{urllib.parse.quote(meeting_id, safe='')}"
            f"/transcripts/{urllib.parse.quote(transcript_id, safe='')}/content"
        )
        try:
            return self.graph.get_text(f"{base}?$format=text/vtt", accept="text/vtt"), True
        except GraphError as exc:
            if not (exc.status == 403 and exc.has("SpeakerAttributionNotAllowed")):
                raise
        # Tenant has "Include speaker attribution" off: same cues, no names.
        text = self.graph.get_text(base, accept="application/vnd.microsoft.graph.transcript+text")
        return text, False

    def resolve_meeting(self, join_url: str) -> str:
        safe = join_url.replace("'", "''")
        data = self.graph.get_json(odata_url(
            f"{self.graph.base}/me/onlineMeetings", {"$filter": f"JoinWebUrl eq '{safe}'"},
        ))
        items = data.get("value") or []
        return str(items[0].get("id") or "") if items else ""

    def write_meeting(
        self,
        ev: dict[str, Any],
        transcript: dict[str, Any],
        vtt: str,
        index: set[str],
        index_path: Path,
        attributed: bool = True,
    ) -> bool:
        tid = str(transcript.get("id") or "")
        start, _ = self.event_bounds(ev)
        started = parse_graph_datetime(transcript.get("createdDateTime")) or start or self.run_at
        local = started.astimezone()
        directory = self.raw_root / "reuniones" / self.empresa()
        stem = f"{local.strftime('%Y-%m-%d')}-{slugify(ev.get('subject'), 'reunion')}"
        md_path, already = self.choose_path(directory, stem, ".md", tid)
        if already:
            if not self.dry_run:
                append_index(index_path, tid)
            index.add(tid)
            return False
        vtt_path = md_path.with_suffix(".vtt")
        attendees = event_people(ev)
        lines = [
            "---",
            "fuente: reunion",
            f"empresa: {self.empresa()}",
            f"cuenta: {yaml_quote(self.cuenta)}",
            f"fecha: {local_iso(started)}",
            f"id_origen: {yaml_quote(tid)}",
            *yaml_list("participantes", attendees),
            f"asunto: {yaml_quote(ev.get('subject') or '')}",
            "proyecto:",
            f"organizador: {yaml_quote(person(ev.get('organizer')))}",
            f"vtt: {yaml_quote(vtt_path.name)}",
            *([] if attributed else ["hablantes: sin-atribucion"]),
            f"ingestado: {self.ingestado}",
            "---",
            "",
            "## Transcripción",
            "",
        ]
        cues = vtt_to_lines(vtt, attributed=attributed)
        lines.extend(cues or ["(transcripción vacía)"])
        content = "\n".join(lines).rstrip("\n") + "\n"
        if self.dry_run:
            self.plan(md_path, "reunión")
            self.plan(vtt_path, "reunión (vtt)")
        else:
            atomic_write_text(vtt_path, sanitize_text(vtt), newline="")
            atomic_write_text(md_path, content)
            append_index(index_path, tid)
        index.add(tid)
        return True


# ---------------------------------------------------------------------------
# WebVTT
# ---------------------------------------------------------------------------

_TIMING_RE = re.compile(r"^\s*(\d+(?::\d+){1,2}(?:[.,]\d+)?)\s+-->\s+")
_VOICE_RE = re.compile(r"<v(?:\.[^\s>]*)?(?:\s+([^>]*))?>(.*?)(?:</v>|(?=<v[\s.>])|$)", re.DOTALL)
_TAG_RE = re.compile(r"</?[^>]+>")


def _cue_start(stamp: str) -> str:
    parts = stamp.replace(",", ".").split(".")[0].split(":")
    try:
        numbers = [int(p) for p in parts]
    except ValueError:
        return "00:00:00"
    while len(numbers) < 3:
        numbers.insert(0, 0)
    hours, minutes, seconds = numbers[-3:]
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _clean_cue_text(text: str) -> str:
    return one_line(html.unescape(_TAG_RE.sub("", text)))


def vtt_to_lines(vtt: str, attributed: bool = True) -> list[str]:
    """`[HH:MM:SS] Nombre Apellido: texto`, one line per intervention.

    Speaker comes from the `<v Name>` tag; cues without one are `Desconocido`.
    `attributed=False` (tenant without speaker attribution) forces `Desconocido`.
    """
    out: list[str] = []
    blocks = re.split(r"\n\s*\n", normalize_newlines(sanitize_text(vtt)).lstrip("﻿"))
    for block in blocks:
        lines = block.split("\n")
        timing_index = next((i for i, line in enumerate(lines) if _TIMING_RE.match(line)), None)
        if timing_index is None:
            continue  # header, NOTE, STYLE, REGION
        stamp = _cue_start(_TIMING_RE.match(lines[timing_index]).group(1))
        payload = " ".join(lines[timing_index + 1:]).strip()
        if not payload:
            continue
        voices = [(m.group(1), m.group(2)) for m in _VOICE_RE.finditer(payload)]
        if voices:
            for speaker, text in voices:
                cleaned = _clean_cue_text(text)
                name = normalize_name(html.unescape(speaker or "")) if attributed else ""
                if cleaned:
                    out.append(f"[{stamp}] {name or 'Desconocido'}: {cleaned}")
        else:
            cleaned = _clean_cue_text(payload)
            if cleaned:
                out.append(f"[{stamp}] Desconocido: {cleaned}")
    return out
