from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from contextlib import contextmanager, redirect_stderr
from datetime import datetime, time as datetime_time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
FETCH = ROOT / "fetch.py"
INGEST = ROOT / "ingest.py"
CLIENT_ID = "11111111-1111-4111-8111-111111111111"
TENANT_ID = "22222222-2222-4222-8222-222222222222"
READ_ONLY_SCOPES = ["User.Read", "Mail.Read", "Calendars.Read", "offline_access"]


class FakeGraphHandler(BaseHTTPRequestHandler):
    server: "FakeGraphServer"

    def log_message(self, _format, *_args):
        return

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.server.paths.append(("POST", self.path))
        if self.path.endswith("/oauth2/v2.0/token"):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("ascii")
            self.server.token_requests.append(urllib.parse.parse_qs(body))
            self._json({
                "access_token": "refreshed-access",
                "refresh_token": "refreshed-refresh",
                "expires_in": 3600,
                "scope": " ".join(READ_ONLY_SCOPES),
            })
            return
        self._json({"error": "unexpected POST"}, 404)

    def do_GET(self):
        self.server.paths.append(("GET", self.path))
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/$metadata":
            self._json({"ok": True})
        elif path == "/me":
            self._json({"displayName": "Test User", "userPrincipalName": "test@example.invalid"})
        elif path == "/me/mailFolders":
            self._json({"value": [{"id": "folder-1", "displayName": "Cerebro"}]})
        elif path == "/me/messages":
            self._json({"value": [self.server.mail_message("search-1", "Búsqueda 🔎")]})
        elif path == "/me/mailFolders/folder-1/messages":
            if self.server.unauthorized_once and not self.server.did_unauthorized:
                self.server.did_unauthorized = True
                self._json({"error": "expired"}, 401)
                return
            next_link = f"{self.server.base_url}/page/mail-2"
            self._json({
                "value": [self.server.mail_message("mail-1", "RE: Factura #12 <urgente>? café 📎")],
                "@odata.nextLink": next_link,
            })
        elif path == "/page/mail-2":
            self._json({"value": [self.server.mail_message("mail-2", "Segundo mensaje")]} )
        elif path in {"/me/calendarView", "/me/calendars/calendar-1/calendarView"}:
            self._json({
                "value": [self.server.calendar_event("event-1", "Revisión ágil")],
                "@odata.nextLink": f"{self.server.base_url}/page/calendar-2",
            })
        elif path == "/page/calendar-2":
            self._json({"value": [self.server.calendar_event("event-2", "Planeación 🚀")]})
        elif path == "/me/calendars":
            self._json({"value": [{
                "id": "calendar-1", "name": "Calendar", "owner": {"address": "test@example.invalid"},
                "canEdit": True, "canShare": False,
            }]})
        else:
            self._json({"error": f"unexpected path {path}"}, 404)


class FakeGraphServer(ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), FakeGraphHandler)
        self.base_url = f"http://127.0.0.1:{self.server_port}"
        self.paths: list[tuple[str, str]] = []
        self.token_requests: list[dict] = []
        self.unauthorized_once = False
        self.did_unauthorized = False

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


@contextmanager
def fake_server():
    try:
        server = FakeGraphServer()
    except PermissionError as exc:
        raise unittest.SkipTest(f"loopback sockets unavailable in this sandbox: {exc}") from exc
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def clean_env(config_dir: Path, server: FakeGraphServer | None = None) -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "MS_GRAPH_CLIENT_ID", "MS_GRAPH_TENANT_ID", "MS_GRAPH_SCOPES",
        "INGEST_OUTLOOK_REDIRECT_PORT", "INGEST_OUTLOOK_READ_ONLY",
        "INGEST_OUTLOOK_OUTPUT_DIR", "INGEST_OUTLOOK_VAULT_ROOT",
        "INGEST_OUTLOOK_GRAPH_BASE", "INGEST_OUTLOOK_AUTHORITY_BASE",
        # The connector must configure its own console encoding; the tests
        # must not force UTF-8 via the parent environment.
        "PYTHONIOENCODING", "PYTHONUTF8",
    ):
        env.pop(key, None)
    env["INGEST_OUTLOOK_CONFIG_DIR"] = str(config_dir)
    if server:
        env["INGEST_OUTLOOK_GRAPH_BASE"] = server.base_url
        env["INGEST_OUTLOOK_AUTHORITY_BASE"] = server.base_url
    return env


def write_config(config_dir: Path, vault: Path, **extra) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "client_id": CLIENT_ID,
        "tenant_id": TENANT_ID,
        "read_only": True,
        "vault_root": str(vault),
    }
    data.update(extra)
    (config_dir / "config.json").write_text(json.dumps(data), encoding="utf-8")


def write_token(config_dir: Path, access_token="seed-access", expired=False) -> None:
    token = {
        "access_token": access_token,
        "refresh_token": "seed-refresh",
        "expires_at": time.time() - 60 if expired else time.time() + 3600,
        "scopes_granted": READ_ONLY_SCOPES,
    }
    (config_dir / "token.json").write_text(json.dumps(token), encoding="utf-8")
    if os.name != "nt":
        os.chmod(config_dir / "token.json", 0o600)


def run_fetch(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(FETCH), *args],
        cwd=ROOT,
        env=env,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=20,
    )


def load_fetch_module(config_dir: Path):
    old = os.environ.get("INGEST_OUTLOOK_CONFIG_DIR")
    os.environ["INGEST_OUTLOOK_CONFIG_DIR"] = str(config_dir)
    try:
        name = f"fetch_test_{time.time_ns()}"
        spec = importlib.util.spec_from_file_location(name, FETCH)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader
        spec.loader.exec_module(module)
        return module
    finally:
        if old is None:
            os.environ.pop("INGEST_OUTLOOK_CONFIG_DIR", None)
        else:
            os.environ["INGEST_OUTLOOK_CONFIG_DIR"] = old


class IngestOutlookV050Tests(unittest.TestCase):
    def test_mail_fetch_and_ingest_preserve_unicode_and_pagination(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server() as server:
            root = Path(tmp)
            config_dir, vault = root / "config", root / "vault"
            vault.mkdir()
            write_config(config_dir, vault)
            write_token(config_dir)
            result = run_fetch(clean_env(config_dir, server), "mail", "--scope", "Cerebro", "--scope-kind", "folder")
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(len(payload["messages"]), 2)
            ingest = subprocess.run(
                [sys.executable, str(INGEST), "--target-date", "2099-01-02"],
                input=result.stdout.encode("utf-8"), capture_output=True, timeout=10,
            )
            self.assertEqual(ingest.returncode, 0, ingest.stderr.decode("utf-8"))
            output = vault / "External Inputs" / "Outlook" / "Mail" / "cerebro" / "2099-01-02.md"
            text = output.read_text(encoding="utf-8")
            self.assertIn("Factura #12 <urgente>? café 📎", text)
            self.assertIn("información 😊", text)
            self.assertTrue(any(path.startswith("/page/mail-2") for _, path in server.paths))

    def test_calendar_ingest_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server() as server:
            root = Path(tmp)
            config_dir, vault = root / "config", root / "vault"
            vault.mkdir()
            write_config(config_dir, vault)
            write_token(config_dir)
            fetched = run_fetch(clean_env(config_dir, server), "calendar")
            self.assertEqual(fetched.returncode, 0, fetched.stderr)
            for _ in range(2):
                ingested = subprocess.run(
                    [sys.executable, str(INGEST), "--target-date", "2099-01-02"],
                    input=fetched.stdout.encode("utf-8"), capture_output=True, timeout=10,
                )
                self.assertEqual(ingested.returncode, 0, ingested.stderr.decode("utf-8"))
            output = vault / "External Inputs" / "Outlook" / "Calendar" / "2099-01-02.md"
            text = output.read_text(encoding="utf-8")
            self.assertEqual(sum(line.startswith("## ") for line in text.splitlines()), 2)
            self.assertEqual(text.count("- **Event ID:** `event-1`"), 1)
            self.assertIn("Planeación 🚀", text)

    @unittest.skipUnless(os.name != "nt", "TZ environment handling is not portable on Windows")
    def test_calendar_naive_utc_timestamp_renders_in_bogota_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = root / "vault"
            vault.mkdir()
            payload = {
                "kind": "calendar",
                "days": 1,
                "date_range": ["2099-01-01", "2099-01-03"],
                "vault_root": str(vault),
                "events": [{
                    "id": "utc-naive",
                    "subject": "Reunión UTC",
                    "start": "2099-01-02T20:00:00.0000000",
                    "end": "2099-01-02T21:00:00.0000000",
                    "timezone": "UTC",
                }],
            }
            env = os.environ.copy()
            env["TZ"] = "America/Bogota"
            ingested = subprocess.run(
                [sys.executable, str(INGEST), "--target-date", "2099-01-02"],
                input=json.dumps(payload).encode("utf-8"),
                env=env,
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(ingested.returncode, 0, ingested.stderr.decode("utf-8"))
            output = vault / "External Inputs" / "Outlook" / "Calendar" / "2099-01-02.md"
            text = output.read_text(encoding="utf-8")
            self.assertIn("date_range: 2099-01-01..2099-01-03", text)
            self.assertIn("# Outlook calendar, 2099-01-01 to 2099-01-03", text)
            self.assertIn("## 2099-01-02 15:00 Reunión UTC", text)
            self.assertIn("- **End:** 2099-01-02 16:00", text)

    def test_calendar_payload_normalizes_only_naive_utc_timestamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = load_fetch_module(Path(tmp) / "config")
            base = {
                "id": "event-utc",
                "subject": "UTC",
                "start": {"dateTime": "2099-01-02T20:00:00.0000000", "timeZone": "uTc"},
                "end": {"dateTime": "2099-01-02T21:00:00.0000000", "timeZone": ""},
            }
            event = module.calendar_events_to_payload([base])[0]
            meeting = module.meetings_to_payload([{"event": base}])[0]
            self.assertEqual(event["start"], "2099-01-02T20:00:00Z")
            self.assertEqual(event["end"], "2099-01-02T21:00:00Z")
            self.assertEqual(meeting["start"], "2099-01-02T20:00:00Z")

            zoned = dict(base)
            zoned["start"] = {
                "dateTime": "2099-01-02T20:00:00.0000000",
                "timeZone": "America/Bogota",
            }
            preserved = module.calendar_events_to_payload([zoned])[0]
            self.assertEqual(preserved["start"], "2099-01-02T20:00:00.0000000")
            self.assertEqual(preserved["timezone"], "America/Bogota")

    def test_calendar_ahead_range_covers_tomorrow(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server() as server:
            root = Path(tmp)
            config_dir, vault = root / "config", root / "vault"
            vault.mkdir()
            write_config(config_dir, vault)
            write_token(config_dir)
            today = datetime.now().astimezone().date()
            fetched = run_fetch(
                clean_env(config_dir, server),
                "calendar", "--days", "1", "--ahead", "1",
            )
            self.assertEqual(fetched.returncode, 0, fetched.stderr)
            payload = json.loads(fetched.stdout)
            self.assertEqual(payload["ahead"], 1)
            self.assertEqual(payload["date_range"], [
                today.isoformat(), (today + timedelta(days=1)).isoformat(),
            ])

            request_path = next(
                path for method, path in server.paths
                if method == "GET" and urllib.parse.urlparse(path).path == "/me/calendarView"
            )
            query = urllib.parse.parse_qs(urllib.parse.urlparse(request_path).query)
            start = datetime.fromisoformat(query["startDateTime"][0].replace("Z", "+00:00"))
            end = datetime.fromisoformat(query["endDateTime"][0].replace("Z", "+00:00"))
            tomorrow = today + timedelta(days=1)
            tomorrow_start = datetime.combine(tomorrow, datetime_time.min).astimezone(timezone.utc)
            tomorrow_end = datetime.combine(
                tomorrow, datetime_time(23, 59, 59)
            ).astimezone(timezone.utc)
            self.assertLessEqual(start, tomorrow_start)
            self.assertGreaterEqual(end, tomorrow_end)

    def test_read_only_blocks_all_writes_including_dry_run_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir, vault = root / "config", root / "vault"
            vault.mkdir()
            write_config(config_dir, vault)
            env = clean_env(config_dir)
            env["INGEST_OUTLOOK_GRAPH_BASE"] = "http://127.0.0.1:1"
            commands = [
                ("send-mail", "--to", "nobody@example.invalid", "--subject", "x", "--dry-run"),
                ("event-create", "--subject", "x", "--start", "2099-01-01T10:00:00", "--end", "2099-01-01T11:00:00", "--dry-run"),
                ("event-update", "--event-id", "e1", "--subject", "x", "--dry-run"),
                ("event-delete", "--event-id", "e1", "--dry-run"),
            ]
            for command in commands:
                result = run_fetch(env, *command)
                self.assertEqual(result.returncode, 3, (command, result.stderr))
                self.assertIn("BLOQUEADO", result.stderr)
            self.assertFalse((config_dir / "token.json").exists())
            logs = (config_dir / "log.jsonl").read_text(encoding="utf-8")
            self.assertEqual(logs.count('"status": "blocked_read_only"'), 4)

    def test_read_only_effective_scopes_and_explicit_env_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir, vault = root / "config", root / "vault"
            write_config(config_dir, vault, teams=False, shared_calendars=False)
            module = load_fetch_module(config_dir)
            with mock.patch.dict(os.environ, clean_env(config_dir), clear=True):
                cfg = module.load_config(require_client=False)
                self.assertEqual(cfg.scopes, READ_ONLY_SCOPES)
                cfg = module.load_config(
                    overrides={"read_only": True, "teams": True, "shared_calendars": True},
                    require_client=False,
                )
                self.assertIn("Calendars.Read.Shared", cfg.scopes)
                self.assertIn("OnlineMeetingTranscript.Read.All", cfg.scopes)
                os.environ["MS_GRAPH_SCOPES"] = "User.Read Mail.Read custom.scope"
                cfg = module.load_config(require_client=False)
                self.assertEqual(cfg.scopes, ["User.Read", "Mail.Read", "custom.scope"])

    def test_doctor_uses_effective_read_only_scopes_without_drift(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server() as server:
            root = Path(tmp)
            config_dir, vault = root / "config", root / "vault"
            vault.mkdir()
            write_config(config_dir, vault)
            write_token(config_dir)
            result = run_fetch(clean_env(config_dir, server), "doctor")
            self.assertNotIn("Missing scopes", result.stdout)
            self.assertIn("[PASS] scopes", result.stdout)

    def test_configure_writes_file_and_precedence_is_arg_env_file_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config"
            env = clean_env(config_dir)
            result = run_fetch(
                env, "configure", "--client-id", CLIENT_ID, "--tenant-id", TENANT_ID,
                "--read-only", "--vault-root", str(root / "vault"), "--show",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            saved = json.loads((config_dir / "config.json").read_text(encoding="utf-8"))
            self.assertTrue(saved["read_only"])
            if os.name != "nt":
                self.assertEqual((config_dir / "config.json").stat().st_mode & 0o777, 0o600)
            module = load_fetch_module(config_dir)
            env["MS_GRAPH_TENANT_ID"] = "organizations"
            with mock.patch.dict(os.environ, env, clear=True):
                cfg = module.load_config(overrides={"tenant_id": "consumers"}, require_client=False)
                self.assertEqual(cfg.tenant_id, "consumers")
                self.assertEqual(cfg.sources["tenant_id"], "arg")
                cfg = module.load_config(require_client=False)
                self.assertEqual(cfg.tenant_id, "organizations")
                self.assertEqual(cfg.sources["tenant_id"], "env")
                os.environ.pop("MS_GRAPH_TENANT_ID")
                cfg = module.load_config(require_client=False)
                self.assertEqual(cfg.tenant_id, TENANT_ID)
                self.assertEqual(cfg.sources["tenant_id"], "file")
            invalid = run_fetch(env, "configure", "--client-id", "not-a-guid")
            self.assertEqual(invalid.returncode, 2)
            self.assertIn("8-4-4-4-12", invalid.stderr)

    def test_custom_output_dir_and_invalid_paths(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server() as server:
            root = Path(tmp)
            config_dir, vault = root / "config", root / "vault"
            vault.mkdir()
            write_config(config_dir, vault, output_dir="Entradas/Correo")
            write_token(config_dir)
            fetched = run_fetch(clean_env(config_dir, server), "calendar")
            self.assertEqual(fetched.returncode, 0, fetched.stderr)
            ingested = subprocess.run(
                [sys.executable, str(INGEST), "--target-date", "2099-01-02"],
                input=fetched.stdout.encode("utf-8"), capture_output=True, timeout=10,
            )
            self.assertEqual(ingested.returncode, 0)
            self.assertTrue((vault / "Entradas" / "Correo" / "Calendar" / "2099-01-02.md").is_file())
            for invalid in ("../escape", str(root / "absolute"), "C:\\escape"):
                result = run_fetch(clean_env(config_dir), "configure", "--output-dir", invalid)
                self.assertEqual(result.returncode, 2, invalid)

    def test_windows_token_permission_check_passes_without_fix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir, vault = root / "config", root / "vault"
            vault.mkdir()
            write_config(config_dir, vault)
            write_token(config_dir)
            module = load_fetch_module(config_dir)
            env = clean_env(config_dir)

            class Response:
                status = 200
                headers = {}

                def __init__(self, body: bytes):
                    self.body = body

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def read(self, size=-1):
                    return self.body if size < 0 else self.body[:size]

            def fake_urlopen(request, timeout=0):
                url = request.full_url
                if url.endswith("/$metadata"):
                    return Response(b"{}")
                if url.endswith("/me"):
                    return Response(b'{"displayName":"Test User"}')
                raise AssertionError(url)

            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(module, "_is_windows", return_value=True), \
                    mock.patch.object(module.urllib.request, "urlopen", side_effect=fake_urlopen):
                results = module.run_checks()
            perms = next(item for item in results if item.name == "token-perms")
            self.assertEqual(perms.status, "PASS")
            self.assertIn("ACL", perms.detail)
            self.assertIsNone(perms.fix_auto)

    def test_windows_safe_slug_reserved_names_and_long_paths(self):
        spec = importlib.util.spec_from_file_location("ingest_test", INGEST)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader
        spec.loader.exec_module(module)
        self.assertEqual(module.slugify('RE: Factura #12 <urgente>?'), "re-factura-12-urgente")
        self.assertEqual(module.slugify("CON"), "item-con")
        parent = Path("x" * 230)
        shortened = module.shorten_slug_for_path(parent, "a" * 100, "2099-01-02.md")
        self.assertLess(len(shortened), 100)
        self.assertNotRegex(shortened, r'[:*?"<>|.]$')

    def test_expired_token_refreshes_against_fake_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server() as server:
            root = Path(tmp)
            config_dir, vault = root / "config", root / "vault"
            vault.mkdir()
            write_config(config_dir, vault)
            write_token(config_dir, expired=True)
            result = run_fetch(clean_env(config_dir, server), "list-calendars")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(server.token_requests), 1)
            token = json.loads((config_dir / "token.json").read_text(encoding="utf-8"))
            self.assertEqual(token["access_token"], "refreshed-access")

    def test_mid_run_401_refreshes_and_retries(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server() as server:
            server.unauthorized_once = True
            root = Path(tmp)
            config_dir, vault = root / "config", root / "vault"
            vault.mkdir()
            write_config(config_dir, vault)
            write_token(config_dir)
            result = run_fetch(clean_env(config_dir, server), "mail", "--scope", "Cerebro", "--scope-kind", "folder")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(server.did_unauthorized)
            self.assertEqual(len(server.token_requests), 1)
            message_calls = [path for method, path in server.paths if method == "GET" and path.startswith("/me/mailFolders/folder-1/messages")]
            self.assertEqual(len(message_calls), 2)


    def test_verify_scopes_ignores_protocol_scopes_case_and_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = load_fetch_module(Path(tmp) / "config")
            ok, missing = module.verify_scopes(
                {"scopes_granted": ["offline_access", "openid", "profile", "email", "Mail.Read"]},
                ["Mail.Read", "offline_access", "openid", "profile", "email"],
            )
            self.assertTrue(ok)
            self.assertEqual(missing, [])
            ok, _missing = module.verify_scopes(
                {"scopes_granted": ["https://graph.microsoft.com/mail.read"]},
                ["Mail.Read"],
            )
            self.assertTrue(ok)
            ok, missing = module.verify_scopes(
                {"scopes_granted": ["Mail.Read"]},
                ["Mail.Read", "Calendars.Read"],
            )
            self.assertFalse(ok)
            self.assertEqual(missing, ["Calendars.Read"])
            ok, missing = module.verify_scopes({"access_token": "legacy"}, ["Mail.Read"])
            self.assertTrue(ok)
            self.assertEqual(missing, [])

    def test_read_only_env_scopes_drop_write_scopes_with_stderr_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir, vault = root / "config", root / "vault"
            write_config(config_dir, vault)
            module = load_fetch_module(config_dir)
            env = clean_env(config_dir)
            env["MS_GRAPH_SCOPES"] = (
                "User.Read Mail.Read Mail.Send Calendars.ReadWrite "
                "OnlineMeetingTranscript.Read.All"
            )
            with mock.patch.dict(os.environ, env, clear=True):
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    cfg = module.load_config(require_client=False)
            self.assertEqual(
                cfg.scopes,
                ["User.Read", "Mail.Read", "OnlineMeetingTranscript.Read.All"],
            )
            self.assertIn("Mail.Send", stderr.getvalue())
            self.assertIn("Calendars.ReadWrite", stderr.getvalue())

    def test_read_only_meetings_blocked_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir, vault = root / "config", root / "vault"
            vault.mkdir()
            write_config(config_dir, vault)
            env = clean_env(config_dir)
            env["INGEST_OUTLOOK_GRAPH_BASE"] = "http://127.0.0.1:1"
            result = run_fetch(env, "meetings")
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertIn("BLOQUEADO", result.stderr)
            self.assertIn("Teams", result.stderr)
            self.assertFalse((config_dir / "token.json").exists())
            logs = (config_dir / "log.jsonl").read_text(encoding="utf-8")
            self.assertEqual(logs.count('"status": "blocked_read_only"'), 1)

    def test_config_bom_tolerated_and_broken_config_reports_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server() as server:
            root = Path(tmp)
            config_dir, vault = root / "config", root / "vault"
            vault.mkdir()
            data = json.dumps({
                "client_id": CLIENT_ID,
                "tenant_id": TENANT_ID,
                "read_only": True,
                "vault_root": str(vault),
            })
            config_dir.mkdir(parents=True, exist_ok=True)
            (config_dir / "config.json").write_bytes(b"\xef\xbb\xbf" + data.encode("utf-8"))
            write_token(config_dir)
            result = run_fetch(clean_env(config_dir, server), "list-calendars")
            self.assertEqual(result.returncode, 0, result.stderr)

            (config_dir / "config.json").write_text("{not valid json", encoding="utf-8")
            broken = run_fetch(clean_env(config_dir, server), "doctor")
            self.assertEqual(broken.returncode, 1, broken.stdout + broken.stderr)
            self.assertIn("config.json inválido", broken.stdout + broken.stderr)
            self.assertNotIn("Traceback", broken.stderr)


if __name__ == "__main__":
    unittest.main()
