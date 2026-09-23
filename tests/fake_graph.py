#!/usr/bin/env python3
"""
Fake Microsoft Graph + token endpoint for ingest-outlook tests and CI.

As a module: `FakeGraphServer` (loopback, random port) and the `fake_server()`
context manager. As a script (CI):

    python tests/fake_graph.py --port 8799 --scenario basic
    python tests/fake_graph.py --seed-token <profile-config-dir> [...]

Only loopback, only synthetic data. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import unittest
import urllib.parse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

READ_ONLY_SCOPES = ["User.Read", "Mail.Read", "Calendars.Read", "offline_access"]
WELL_KNOWN = {
    "inbox": ("folder-inbox", "Bandeja de entrada"),
    "junkemail": ("folder-junk", "Correo no deseado"),
    "deleteditems": ("folder-deleted", "Elementos eliminados"),
    "drafts": ("folder-drafts", "Borradores"),
    "sentitems": ("folder-sent", "Elementos enviados"),
    "msgfolderroot": ("folder-root", "Top of Information Store"),
}


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def graph_dt(dt: datetime) -> dict[str, str]:
    """Graph calendar shape: naive UTC with 7-digit fraction."""
    return {
        "dateTime": dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000"),
        "timeZone": "UTC",
    }


def address(name: str, email: str) -> dict[str, Any]:
    return {"emailAddress": {"name": name, "address": email}}


def make_message(
    message_id: str,
    subject: str,
    received: datetime,
    folder: str = "folder-inbox",
    sender: tuple[str, str] = ("Ángela Pérez", "angela@example.invalid"),
    to: list[tuple[str, str]] | None = None,
    cc: list[tuple[str, str]] | None = None,
    body: str = "Texto con acentos: información 😊",
    has_attachments: bool = False,
    conversation: str = "conv-1",
    immutable_id: str | None = None,
    internet_id: str | None = None,
) -> dict[str, Any]:
    """`id` is the REST id (changes when the message moves); `_immutable` is
    what Graph returns with Prefer IdType="ImmutableId" (stable)."""
    return {
        "id": message_id,
        "_immutable": immutable_id or message_id,
        "internetMessageId": internet_id or f"<{message_id}@example.invalid>",
        "conversationId": conversation,
        "parentFolderId": folder,
        "subject": subject,
        "receivedDateTime": iso_z(received),
        "from": address(*sender),
        "sender": address(*sender),
        "toRecipients": [address(n, e) for n, e in (to or [("Test User", "test@example.invalid")])],
        "ccRecipients": [address(n, e) for n, e in (cc or [])],
        "hasAttachments": has_attachments,
        "body": {"contentType": "text", "content": body},
    }


def make_event(
    event_id: str,
    subject: str,
    start: datetime,
    end: datetime,
    organizer: tuple[str, str] = ("Ángela Pérez", "angela@example.invalid"),
    attendees: list[dict[str, Any]] | None = None,
    location: str = "Sala 1",
    teams_join: str | None = None,
    cancelled: bool = False,
    all_day: bool = False,
    body: str = "Agenda de la reunión",
) -> dict[str, Any]:
    event = {
        "id": event_id,
        "subject": subject,
        "start": graph_dt(start),
        "end": graph_dt(end),
        "organizer": address(*organizer),
        "attendees": attendees or [],
        "location": {"displayName": location},
        "isAllDay": all_day,
        "isCancelled": cancelled,
        "isOnlineMeeting": bool(teams_join),
        "onlineMeetingProvider": "teamsForBusiness" if teams_join else "unknown",
        "onlineMeeting": {"joinUrl": teams_join} if teams_join else None,
        "body": {"contentType": "text", "content": body},
        "bodyPreview": body[:255],
    }
    if all_day:
        day = start.date()
        event["start"] = {"dateTime": f"{day.isoformat()}T00:00:00.0000000", "timeZone": "UTC"}
        last = end.date()
        event["end"] = {"dateTime": f"{last.isoformat()}T00:00:00.0000000", "timeZone": "UTC"}
    return event


def attendee(name: str, email: str, kind: str = "required") -> dict[str, Any]:
    return {"type": kind, "status": {"response": "accepted"}, **address(name, email)}


class FakeGraphHandler(BaseHTTPRequestHandler):
    server: "FakeGraphServer"

    def log_message(self, _format, *_args):
        return

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, status=200):
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, status: int, code: str, message: str = "", inner: str | None = None):
        error: dict[str, Any] = {"code": code, "message": message or code}
        if inner:
            error["innerError"] = {"code": inner, "date": "2099-01-01T00:00:00"}
        self._json({"error": error}, status)

    def do_POST(self):
        self.server.paths.append(("POST", self.path))
        if self.path.endswith("/oauth2/v2.0/token"):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("ascii")
            self.server.token_requests.append(urllib.parse.parse_qs(body))
            if self.server.token_fail:
                self._json({
                    "error": "invalid_grant",
                    "error_description": "AADSTS70008: The provided authorization code or refresh token has expired.",
                }, 400)
                return
            self._json({
                "access_token": "refreshed-access",
                "refresh_token": "refreshed-refresh",
                "expires_in": 3600,
                "scope": " ".join(READ_ONLY_SCOPES),
            })
            return
        self.server.write_calls.append(self.path)
        self._json({"error": "unexpected POST"}, 405)

    def do_PATCH(self):
        self.server.write_calls.append(self.path)
        self._json({"error": "unexpected PATCH"}, 405)

    def do_DELETE(self):
        self.server.write_calls.append(self.path)
        self._json({"error": "unexpected DELETE"}, 405)

    def do_GET(self):  # noqa: C901 - a router
        server = self.server
        server.paths.append(("GET", self.path))
        server.accepts.append(self.headers.get("Accept", ""))
        server.prefers.append(self.headers.get("Prefer", ""))
        parsed = urllib.parse.urlparse(self.path)
        raw_path = parsed.path
        segments = [urllib.parse.unquote(s) for s in raw_path.split("/") if s]
        query = urllib.parse.parse_qs(parsed.query)
        for prefix, delay in server.delays.items():
            if raw_path.startswith(prefix):
                time.sleep(delay)
        if server.on_request is not None:
            server.on_request(raw_path)
        for prefix, status in server.fail_paths.items():
            if raw_path == prefix:
                self._error(status, "Forbidden" if status == 403 else "ServiceUnavailable")
                return
        if raw_path in server.raw_responses:
            status, body = server.raw_responses[raw_path]
            self._send(status, body.encode("utf-8"), "application/json; charset=utf-8")
            return

        if server.unauthorized_once and not server.did_unauthorized and raw_path in server.unauthorized_paths:
            server.did_unauthorized = True
            self._json({"error": "expired"}, 401)
            return
        if server.always_401:
            self._error(401, "InvalidAuthenticationToken")
            return

        if raw_path == "/$metadata":
            self._json({"ok": True})
        elif raw_path == "/me":
            self._json({"displayName": "Test User", "userPrincipalName": server.upn, "mail": server.upn})
        elif segments[:2] == ["me", "mailFolders"]:
            self._mail_folders(segments[2:], query)
        elif segments[:2] == ["me", "messages"]:
            self._messages(segments[2:], query)
        elif raw_path == "/page/mail-2":
            self._json({"value": [self.server.mail_message("mail-2", "Segundo mensaje")]})
        elif raw_path in {"/me/calendarView", "/me/calendars/calendar-1/calendarView"}:
            self._calendar_view(query)
        elif raw_path == "/page/calendar-2":
            self._json({"value": [self.server.calendar_event("event-2", "Planeación 🚀")]})
        elif raw_path == "/me/calendars":
            self._json({"value": [{
                "id": "calendar-1", "name": "Calendar", "owner": {"address": "test@example.invalid"},
                "canEdit": True, "canShare": False,
            }]})
        elif segments[:2] == ["me", "onlineMeetings"]:
            self._online_meetings(segments[2:], query)
        else:
            self._json({"error": f"unexpected path {raw_path}"}, 404)

    # -- mail ----------------------------------------------------------------

    def _mail_folders(self, rest: list[str], query: dict[str, list[str]]):
        server = self.server
        if not rest:
            self._json({"value": [{"id": "folder-1", "displayName": "Cerebro"}]})
            return
        key = rest[0]
        if len(rest) >= 2 and rest[1] == "messages":
            if server.unauthorized_once and not server.did_unauthorized:
                server.did_unauthorized = True
                self._json({"error": "expired"}, 401)
                return
            self._json({
                "value": [server.mail_message("mail-1", "RE: Factura #12 <urgente>? café 📎")],
                "@odata.nextLink": f"{server.base_url}/page/mail-2",
            })
            return
        if key in WELL_KNOWN:
            folder_id, name = WELL_KNOWN[key]
            if key in server.missing_well_known:
                self._error(404, "ErrorFolderNotFound")
                return
            self._json({"id": folder_id, "displayName": name, "parentFolderId": "folder-root"})
            return
        folder = server.folders.get(key)
        if folder:
            self._json(folder)
            return
        self._error(404, "ErrorItemNotFound")

    def _immutable(self) -> bool:
        return 'IdType="ImmutableId"' in (self.headers.get("Prefer") or "")

    def _present(self, message: dict[str, Any]) -> dict[str, Any]:
        shown = {k: v for k, v in message.items() if not k.startswith("_")}
        if self._immutable():
            shown["id"] = message.get("_immutable", message["id"])
        return shown

    def _find(self, message_id: str) -> dict[str, Any] | None:
        key = "_immutable" if self._immutable() else "id"
        return next((m for m in self.server.messages if m.get(key, m["id"]) == message_id), None)

    def _messages(self, rest: list[str], query: dict[str, list[str]]):
        server = self.server
        if rest:
            message_id = rest[0]
            if len(rest) >= 2 and rest[1] == "attachments":
                found = self._find(message_id)
                keys = [message_id] + ([found["id"], found.get("_immutable")] if found else [])
                value = next((server.attachments[k] for k in keys if k in server.attachments), [])
                self._json({"value": value})
                return
            found = self._find(message_id)
            if len(rest) == 1 and found is not None:
                self._json(self._present(found))
                return
            self._error(404, "ErrorItemNotFound")
            return
        if "$search" in query:
            self._json({"value": [server.mail_message("search-1", "Búsqueda 🔎")]})
            return
        since = ""
        filt = (query.get("$filter") or [""])[0]
        if " ge " in filt:
            since = filt.split(" ge ", 1)[1].strip()
        items = sorted(
            (m for m in server.messages if m["receivedDateTime"] >= since),
            key=lambda m: (m["receivedDateTime"], m["id"]),
        )
        top = int((query.get("$top") or ["50"])[0])
        skip = int((query.get("$skip") or ["0"])[0])
        page = [self._present(m) for m in items[skip: skip + top]]
        payload: dict[str, Any] = {"value": page}
        if skip + top < len(items):
            params = {k: v[0] for k, v in query.items()}
            params["$skip"] = str(skip + top)
            payload["@odata.nextLink"] = f"{server.base_url}/me/messages?" + urllib.parse.urlencode(
                params, quote_via=urllib.parse.quote
            )
        self._json(payload)

    # -- calendar ------------------------------------------------------------

    def _calendar_view(self, query: dict[str, list[str]]):
        server = self.server
        server.calendar_queries.append({k: v[0] for k, v in query.items()})
        if server.events is None:
            self._json({
                "value": [server.calendar_event("event-1", "Revisión ágil")],
                "@odata.nextLink": f"{server.base_url}/page/calendar-2",
            })
            return
        top = int((query.get("$top") or ["50"])[0])
        skip = int((query.get("$skip") or ["0"])[0])
        items = list(server.events)
        page = items[skip: skip + top]
        payload: dict[str, Any] = {"value": page}
        if skip + top < len(items):
            params = {k: v[0] for k, v in query.items()}
            params["$skip"] = str(skip + top)
            payload["@odata.nextLink"] = f"{server.base_url}/me/calendarView?" + urllib.parse.urlencode(
                params, quote_via=urllib.parse.quote
            )
        self._json(payload)

    # -- Teams ---------------------------------------------------------------

    def _online_meetings(self, rest: list[str], query: dict[str, list[str]]):
        server = self.server
        if not rest:
            filt = (query.get("$filter") or [""])[0]
            join = ""
            if "JoinWebUrl eq '" in filt:
                join = filt.split("JoinWebUrl eq '", 1)[1].rsplit("'", 1)[0].replace("''", "'")
            meeting_id = server.online_meetings.get(join)
            self._json({"value": [{"id": meeting_id, "joinWebUrl": join}] if meeting_id else []})
            return
        meeting_id = rest[0]
        if len(rest) == 2 and rest[1] == "transcripts":
            if server.transcripts_forbidden:
                self._error(403, "Forbidden", "Access to transcripts is disabled",
                            inner="GraphAccessToTranscriptsDisabled")
                return
            self._json({"value": server.transcripts.get(meeting_id, [])})
            return
        if len(rest) == 4 and rest[1] == "transcripts" and rest[3] == "content":
            accept = self.headers.get("Accept", "")
            if server.speaker_attribution_off and "text/vtt" in accept:
                self._error(403, "Forbidden", "Speaker attribution is not allowed",
                            inner="SpeakerAttributionNotAllowed")
                return
            content = server.transcript_content.get(rest[2])
            if content is None:
                self._error(404, "NotFound")
                return
            ctype = "text/vtt" if "text/vtt" in accept else "text/plain"
            self._send(200, content.encode("utf-8"), f"{ctype}; charset=utf-8")
            return
        self._error(404, "NotFound")


class FakeGraphServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # A client that timed out closes the socket while a delayed handler is
        # still writing (BrokenPipe/ConnectionReset): expected in timeout tests.
        return

    def __init__(self, port: int = 0):
        super().__init__(("127.0.0.1", port), FakeGraphHandler)
        self.base_url = f"http://127.0.0.1:{self.server_port}"
        self.paths: list[tuple[str, str]] = []
        self.accepts: list[str] = []
        self.prefers: list[str] = []
        self.token_requests: list[dict] = []
        self.write_calls: list[str] = []
        self.calendar_queries: list[dict[str, str]] = []
        self.unauthorized_once = False
        self.unauthorized_paths: set[str] = set()
        self.did_unauthorized = False
        self.always_401 = False
        self.token_fail = False
        self.upn = "test@example.invalid"
        # Cerebro scenario data (None/empty = legacy v0.5 behavior).
        self.messages: list[dict[str, Any]] = []
        self.attachments: dict[str, list[dict[str, Any]]] = {}
        self.folders: dict[str, dict[str, Any]] = {}
        self.missing_well_known: set[str] = set()
        self.events: list[dict[str, Any]] | None = None
        self.online_meetings: dict[str, str] = {}
        self.transcripts: dict[str, list[dict[str, Any]]] = {}
        self.transcript_content: dict[str, str] = {}
        self.transcripts_forbidden = False
        self.speaker_attribution_off = False
        self.delays: dict[str, float] = {}
        self.on_request = None  # optional callable(path) for tests
        self.fail_paths: dict[str, int] = {}  # exact path -> HTTP status
        self.raw_responses: dict[str, tuple[int, str]] = {}  # exact path -> (status, raw body)

    @staticmethod
    def mail_message(message_id: str, subject: str):
        return {
            "id": message_id,
            "subject": subject,
            "receivedDateTime": "2099-01-02T12:00:00Z",
            "from": {"emailAddress": {"name": "Ángela", "address": "sender@example.invalid"}},
            "toRecipients": [{"emailAddress": {"address": "test@example.invalid"}}],
            "ccRecipients": [],
            "body": {"contentType": "text", "content": "Texto con acentos: información 😊"},
            "categories": [],
        }

    @staticmethod
    def calendar_event(event_id: str, subject: str):
        return {
            "id": event_id,
            "subject": subject,
            "start": {"dateTime": "2099-01-02T14:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2099-01-02T15:00:00Z", "timeZone": "UTC"},
            "organizer": {"emailAddress": {"name": "Ángela", "address": "sender@example.invalid"}},
            "attendees": [],
            "body": {"contentType": "text", "content": "Agenda"},
            "location": {"displayName": "Sala 1"},
            "categories": [],
        }


def local_at(days_offset: int, hour: int, minute: int = 0) -> datetime:
    """Local wall-clock time `days_offset` days from today (aware)."""
    today = datetime.now().astimezone().date() + timedelta(days=days_offset)
    return datetime(today.year, today.month, today.day, hour, minute).astimezone()


def build_basic_scenario(server: FakeGraphServer) -> None:
    """Small mailbox, a calendar with a cancelled event, no Teams."""
    server.folders["folder-inbox"] = {"id": "folder-inbox", "displayName": "Bandeja de entrada",
                                      "parentFolderId": "folder-root"}
    server.folders["folder-proyectos"] = {"id": "folder-proyectos", "displayName": "Proyectos",
                                          "parentFolderId": "folder-inbox"}
    server.folders["folder-deleted-sub"] = {"id": "folder-deleted-sub", "displayName": "Viejo",
                                            "parentFolderId": "folder-deleted"}
    server.messages = [
        make_message("AAMk-msg-1/x+y==", 'RE: Factura: #12 "urgente"\ncafé 📎', local_at(-2, 9, 15),
                     cc=[("Test User", "TEST@example.invalid"), ("Luis Gómez", "luis@example.invalid")],
                     has_attachments=True),
        make_message("AAMk-msg-2", "Plan de proyecto", local_at(-2, 9, 15), folder="folder-proyectos",
                     sender=("Luis Gómez", "luis@example.invalid")),
        make_message("AAMk-junk", "Gane dinero", local_at(-1, 8, 0), folder="folder-junk"),
        make_message("AAMk-draft", "Borrador sin enviar", local_at(-1, 8, 30), folder="folder-drafts"),
        make_message("AAMk-deleted", "Viejo borrado", local_at(-1, 8, 45), folder="folder-deleted-sub"),
        make_message("AAMk-msg-3", "Minuta ---", local_at(-1, 10, 5),
                     body="Hola\n---\nlínea después de regla"),
    ]
    server.attachments["AAMk-msg-1/x+y=="] = [
        {"name": "factura.pdf", "contentType": "application/pdf", "size": 120_000},
    ]
    server.events = [
        make_event("EV-1", "Comité: revisión", local_at(0, 9, 0), local_at(0, 10, 0),
                   attendees=[attendee("Luis Gómez", "luis@example.invalid"),
                              attendee("Sala Norte", "sala.norte@example.invalid", "resource")]),
        make_event("EV-2", "Cancelada", local_at(1, 11, 0), local_at(1, 11, 30), cancelled=True),
        make_event("EV-3", "Offsite", local_at(2, 0, 0), local_at(4, 0, 0), all_day=True),
    ]


SCENARIOS = {"basic": build_basic_scenario}


@contextmanager
def fake_server(scenario: str | None = None):
    try:
        server = FakeGraphServer()
    except PermissionError as exc:
        raise unittest.SkipTest(f"loopback sockets unavailable in this sandbox: {exc}") from exc
    if scenario:
        SCENARIOS[scenario](server)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def seed_token(config_dir: Path, expired: bool = True, scopes: list[str] | None = None) -> Path:
    """Token for the fake endpoint. Expired by default so the first call refreshes."""
    config_dir.mkdir(parents=True, exist_ok=True)
    token = {
        "access_token": "seed-access",
        "refresh_token": "seed-refresh",
        "expires_at": time.time() - 60 if expired else time.time() + 3600,
        "scopes_granted": scopes or READ_ONLY_SCOPES,
    }
    path = config_dir / "token.json"
    path.write_text(json.dumps(token), encoding="utf-8")
    if os.name != "nt":
        os.chmod(path, 0o600)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fake Microsoft Graph for ingest-outlook tests.")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="basic")
    parser.add_argument("--seed-token", nargs="+", default=None, metavar="CONFIG_DIR",
                        help="Write a fake token.json into each profile config dir and exit.")
    args = parser.parse_args(argv)
    if args.seed_token:
        for directory in args.seed_token:
            print(seed_token(Path(directory)))
        return 0
    server = FakeGraphServer(args.port)
    SCENARIOS[args.scenario](server)
    print(f"fake graph listening on {server.base_url} (scenario {args.scenario})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
