"""v0.6.0: profiles, cerebro layout `sync`, vault lock, Windows schedule.

Everything runs against the loopback fake Graph (tests/fake_graph.py) and
temporary directories. No real accounts, no real network.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake_graph  # noqa: E402
from fake_graph import (  # noqa: E402
    attendee,
    fake_server,
    local_at,
    make_event,
    make_message,
    seed_token,
)
from support import (  # noqa: E402
    CLIENT_ID,
    FETCH,
    LOCK_CLI,
    ROOT,
    TENANT_ID,
    clean_env,
    load_fetch_module,
    run_fetch,
)

sys.path.insert(0, str(ROOT))
import cerebro_lock  # noqa: E402
import schedule_mac  # noqa: E402
import schedule_win  # noqa: E402
import sync_cerebro  # noqa: E402

DEFAULT_RAW = Path("raw") / "entradas"
DEFAULT_LOG = Path(".claude") / "system3" / "logs" / "ingesta.log"
DEFAULT_LOCK = Path(".claude") / "system3" / "cerebro.lock"
VTT = (
    "WEBVTT\n\n"
    "0f1e2d3c-1\n00:00:05.000 --> 00:00:08.000\n<v Ana Pérez>Hola a todos, arrancamos.</v>\n\n"
    "00:01:02.500 --> 00:01:05.000\n<v Luis Gómez>Me comprometo a enviar\nel informe el viernes.</v>\n\n"
    "00:02:00.000 --> 00:02:03.000\nSin hablante identificado\n"
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse the frontmatter shape the connector writes (a YAML subset whose
    quoted scalars are JSON strings). Fails loudly on anything else."""
    lines = text.split("\n")
    assert lines[0] == "---", "frontmatter must start at line 1"
    end = lines.index("---", 1)
    data: dict = {}
    key = None
    for line in lines[1:end]:
        if line.startswith("  - "):
            assert key is not None, line
            if data[key] is None:
                data[key] = []
            data[key].append(json.loads(line[4:]))
            continue
        assert ":" in line and not line.startswith(" "), line
        key, _, raw = line.partition(":")
        raw = raw.strip()
        if raw == "":
            data[key] = None
        elif raw == "[]":
            data[key] = []
        elif raw.startswith('"'):
            data[key] = json.loads(raw)
        else:
            data[key] = raw
    return data, "\n".join(lines[end + 1:])


def configure_profile(env: dict, profile: str, vault: Path, *extra: str, empresa: str | None = None):
    args = [
        "configure", "--profile", profile, "--layout", "cerebro",
        "--empresa", empresa or profile,
        "--client-id", CLIENT_ID, "--tenant-id", TENANT_ID, "--read-only",
        "--vault-root", str(vault), *extra,
    ]
    result = run_fetch(env, *args)
    assert result.returncode == 0, result.stderr
    return result


def md_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.md"))


def snapshot(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


class SyncEnv:
    """One temp config base + vault + fake server for a test."""

    def __init__(self, tmp: str, server, profiles=("acme",), vault_name="vault con tildé [x]"):
        self.root = Path(tmp)
        self.config = self.root / "config"
        self.vault = self.root / vault_name
        self.vault.mkdir(parents=True)
        self.server = server
        self.env = clean_env(self.config, server)
        self.env["CEREBRO_LOCK_POLL_SECONDS"] = "0.2"
        for profile in profiles:
            configure_profile(self.env, profile, self.vault)
            seed_token(self.config / profile)

    def sync(self, *args: str, env: dict | None = None, timeout: float = 90):
        return run_fetch(env or self.env, "sync", *args, timeout=timeout)

    def log_lines(self, rel: Path = DEFAULT_LOG) -> list[str]:
        path = self.vault / rel
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def mail_dir(self, empresa="acme", raw=DEFAULT_RAW) -> Path:
        return self.vault / raw / "correo" / empresa


def expected_mail_path(env: SyncEnv, message: dict, slug: str, empresa="acme", raw=DEFAULT_RAW) -> Path:
    received = sync_cerebro.parse_graph_datetime(message["receivedDateTime"]).astimezone()
    return env.vault / raw / "correo" / empresa / received.strftime("%Y-%m-%d") / f"{received.strftime('%H%M')}-{slug}.md"


# ---------------------------------------------------------------------------
# unit tests (no sockets)
# ---------------------------------------------------------------------------

class UnitTests(unittest.TestCase):
    def test_participants_are_normalized_strictly(self):
        msg = {
            "from": fake_graph.address("  'Pérez, Ana'  ", " Ana.Perez@Example.INVALID "),
            "toRecipients": [
                fake_graph.address("luis@example.invalid", "LUIS@example.invalid"),
                fake_graph.address("", "sin.nombre@example.invalid"),
                fake_graph.address("Sin correo", ""),
                fake_graph.address("\"Gómez   <Luis>\"", "luis@example.invalid"),
            ],
            "ccRecipients": [
                fake_graph.address("Ana otra vez", "ana.perez@example.invalid"),
                fake_graph.address("“María  José”", "maria@example.invalid"),
            ],
        }
        people = sync_cerebro.unique_people(
            [sync_cerebro.person(msg["from"])]
            + [sync_cerebro.person(p) for p in msg["toRecipients"]]
            + [sync_cerebro.person(p) for p in msg["ccRecipients"]]
        )
        self.assertEqual(people, [
            "Pérez, Ana <ana.perez@example.invalid>",
            "<luis@example.invalid>",
            "<sin.nombre@example.invalid>",
            "María José <maria@example.invalid>",
        ])
        event = {
            "organizer": fake_graph.address("Luis Gómez", "Luis@Example.invalid"),
            "attendees": [
                attendee("Sala Norte", "sala.norte@example.invalid", "resource"),
                attendee("Ana", "ana@example.invalid"),
                attendee("Luis G.", "luis@example.invalid", "optional"),
            ],
            "location": {"displayName": "Piso 3"},
        }
        self.assertEqual(sync_cerebro.event_people(event), [
            "Luis Gómez <luis@example.invalid>", "Ana <ana@example.invalid>",
        ])
        self.assertEqual(sync_cerebro.event_place(event), "Piso 3; Sala Norte")

    def test_yaml_quote_survives_hostile_subjects(self):
        hostile = 'RE: Factura: #12 "urgente"\n---\nlínea\u2028sep\x85nel\x07bell \\ fin'
        quoted = sync_cerebro.yaml_quote(hostile)
        self.assertNotIn("\n", quoted)
        self.assertEqual(json.loads(quoted), hostile)

    def test_vtt_parser_formats_and_speakers(self):
        self.assertEqual(sync_cerebro.vtt_to_lines(VTT), [
            "[00:00:05] Ana Pérez: Hola a todos, arrancamos.",
            "[00:01:02] Luis Gómez: Me comprometo a enviar el informe el viernes.",
            "[00:02:00] Desconocido: Sin hablante identificado",
        ])
        loud = "WEBVTT\n\n1:02:03.000 --> 1:02:04.000\n<v.loud Carlos &amp; Co>Sí &lt;claro&gt;\n"
        self.assertEqual(sync_cerebro.vtt_to_lines(loud), ["[01:02:03] Carlos & Co: Sí <claro>"])
        unattributed = "0:0:5.0 --> 0:0:8.0\nHola a todos\n\n0:1:2.5 --> 0:1:5.0\n<v Ana>texto</v>\n"
        self.assertEqual(sync_cerebro.vtt_to_lines(unattributed, attributed=False), [
            "[00:00:05] Desconocido: Hola a todos",
            "[00:01:02] Desconocido: texto",
        ])

    def test_slug_and_body_helpers(self):
        self.assertEqual(sync_cerebro.slugify('RE: Revisión "Q3" — café'), "re-revision-q3-cafe")
        self.assertEqual(sync_cerebro.slugify(""), "sin-asunto")
        self.assertEqual(sync_cerebro.slugify("con"), "item-con")
        self.assertLessEqual(len(sync_cerebro.slugify("palabra " * 40)), sync_cerebro.MAX_SLUG_CHARS)
        html_msg = {"body": {"contentType": "html", "content": "<p>Hola<br>mundo</p><style>x{}</style><ul><li>uno</li></ul>"}}
        self.assertEqual(sync_cerebro.body_text(html_msg), "Hola\nmundo\n\n- uno")
        text, truncated = sync_cerebro.truncate_utf8("ñ" * 60_000, sync_cerebro.MAIL_BODY_MAX_BYTES)
        self.assertTrue(truncated)
        self.assertLessEqual(len(text.encode("utf-8")), sync_cerebro.MAIL_BODY_MAX_BYTES)

    def test_empresa_must_be_a_slug(self):
        for bad in ("", None, "Acme", "a b", "con", "x" * 33):
            with self.assertRaises(ValueError):
                sync_cerebro.validate_empresa(bad)
        self.assertEqual(sync_cerebro.validate_empresa("globex"), "globex")

    def test_schedule_script_construction(self):
        spec = schedule_win.ScheduleSpec(
            execute=r"C:\Users\O'Brien\AppData\Local\Programs\Python\Python312\pythonw.exe",
            arguments=schedule_win.build_action_arguments(
                r"C:\cerebros\gerencia\.claude\skills\ingest-outlook\fetch.py", None, "C:\\cerebros\\gerencia"
            ),
            working_dir=r"C:\cerebros\gerencia\.claude\skills\ingest-outlook",
        )
        script = schedule_win.build_register_script(spec)
        self.assertIn("Register-ScheduledTask", script)
        self.assertIn("-TaskPath $folder", script)
        self.assertIn("'\\Rewired\\'", script)
        self.assertIn("$taskName = 'Cerebro - ingesta'", script)
        self.assertIn("-LogonType Interactive -RunLevel Limited", script)
        self.assertIn("-Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '07:00'", script)
        self.assertIn("-RepetitionInterval (New-TimeSpan -Minutes 30)", script)
        self.assertIn("-RepetitionDuration (New-TimeSpan -Minutes 721)", script)
        self.assertIn("-StartWhenAvailable -MultipleInstances IgnoreNew", script)
        self.assertIn("-ExecutionTimeLimit (New-TimeSpan -Minutes 20)", script)
        self.assertIn("O''Brien", script)  # apostrophe escaped for PowerShell
        self.assertNotIn("-Password", script)
        self.assertIn("sync --all --vault-root C:\\cerebros\\gerencia", script)
        self.assertEqual(schedule_win.win_quote_arg("C:\\"), "C:\\")
        self.assertEqual(schedule_win.win_quote_arg("C:\\con espacio\\"), '"C:\\con espacio\\\\"')
        self.assertEqual(
            schedule_win.build_action_arguments("C:\\a b\\fetch.py", "acme"),
            '"C:\\a b\\fetch.py" --profile acme sync',
        )
        command = schedule_win.powershell_command(script)
        self.assertIn("-EncodedCommand", command)
        with self.assertRaises(ValueError):
            schedule_win.validate_start("7am")


# ---------------------------------------------------------------------------
# cerebro_lock (module + CLI)
# ---------------------------------------------------------------------------

class CerebroLockTests(unittest.TestCase):
    def run_cli(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(LOCK_CLI), *args], capture_output=True, text=True,
            encoding="utf-8", timeout=30, env=env or os.environ.copy(),
        )

    def test_cli_acquire_release_status_and_busy(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault con espacio"
            status = self.run_cli("status", "--vault", str(vault))
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertEqual(json.loads(status.stdout)["estado"], "libre")

            acquired = self.run_cli("acquire", "--vault", str(vault), "--paso", "extraccion", "--pid", "4242")
            self.assertEqual(acquired.returncode, 0, acquired.stderr)
            info = json.loads(acquired.stdout)
            self.assertEqual(info["paso"], "extraccion")
            self.assertEqual(info["pid"], 4242)
            self.assertTrue((vault / DEFAULT_LOCK).is_file())
            self.assertEqual(set(json.loads((vault / DEFAULT_LOCK).read_text(encoding="utf-8"))),
                             {"paso", "perfil", "pid", "inicio", "equipo"})

            status = json.loads(self.run_cli("status", "--vault", str(vault)).stdout)
            self.assertEqual(status["estado"], "tomado")

            busy = self.run_cli("acquire", "--vault", str(vault), "--paso", "s3", "--wait", "0.3",
                                "--log", ".claude/system3/logs/s3.log")
            self.assertEqual(busy.returncode, 3)
            self.assertIn("saltada: lock ocupado por extraccion", busy.stderr)
            self.assertIn("saltada", (vault / ".claude/system3/logs/s3.log").read_text(encoding="utf-8"))

            refused = self.run_cli("release", "--vault", str(vault), "--paso", "s3")
            self.assertEqual(refused.returncode, 1)
            self.assertTrue((vault / DEFAULT_LOCK).exists())

            released = self.run_cli("release", "--vault", str(vault), "--paso", "extraccion")
            self.assertEqual(released.returncode, 0, released.stderr)
            self.assertFalse((vault / DEFAULT_LOCK).exists())

    def test_cli_custom_lock_path_and_stale_steal(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            rel = "30-registros/.cerebro.lock"
            first = self.run_cli("acquire", "--vault", str(vault), "--paso", "extraccion", "--lock-path", rel)
            self.assertEqual(first.returncode, 0, first.stderr)
            time.sleep(0.3)
            stolen = self.run_cli("acquire", "--vault", str(vault), "--paso", "ingesta", "--lock-path", rel,
                                  "--stale", "0.1", "--wait", "1")
            self.assertEqual(stolen.returncode, 0, stolen.stderr)
            self.assertIn("lock abandonado robado", stolen.stderr)
            self.assertEqual(json.loads((vault / rel).read_text(encoding="utf-8"))["paso"], "ingesta")
            bad = self.run_cli("status", "--vault", str(vault), "--lock-path", "../fuera.lock")
            self.assertEqual(bad.returncode, 2)

    def test_module_release_only_removes_own_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            notes: list[str] = []
            lock = cerebro_lock.CerebroLock(tmp, "ingesta", perfil="acme", note=notes.append)
            self.assertTrue(lock.acquire())
            path = lock.path
            # someone stole it (e.g. after 20 min) -> our release must not delete theirs
            path.write_text(json.dumps({"paso": "extraccion", "pid": 1, "inicio": "2099-01-01T00:00:00+00:00",
                                        "equipo": "otro"}), encoding="utf-8")
            self.assertFalse(lock.release())
            self.assertTrue(path.exists())
            self.assertTrue(any("ya no era" in n for n in notes))


# ---------------------------------------------------------------------------
# sync against the fake Graph
# ---------------------------------------------------------------------------

class SyncTests(unittest.TestCase):
    def test_sync_writes_one_file_per_message_with_contract_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server("basic") as server:
            env = SyncEnv(tmp, server)
            result = env.sync("--profile", "acme")
            self.assertEqual(result.returncode, 0, result.stderr)

            msg1 = server.messages[0]
            path = expected_mail_path(env, msg1, "re-factura-12-urgente-cafe")
            self.assertTrue(path.is_file(), sorted(map(str, md_files(env.vault))))
            text = path.read_text(encoding="utf-8")
            meta, body = parse_frontmatter(text)
            self.assertEqual(meta["fuente"], "correo")
            self.assertEqual(meta["empresa"], "acme")
            self.assertEqual(meta["cuenta"], "test@example.invalid")
            self.assertEqual(meta["id_origen"], "AAMk-msg-1/x+y==")
            self.assertEqual(meta["asunto"], 'RE: Factura: #12 "urgente"\ncafé 📎')
            self.assertEqual(meta["participantes"], [
                "Ángela Pérez <angela@example.invalid>",
                "Test User <test@example.invalid>",
                "Luis Gómez <luis@example.invalid>",
            ])
            self.assertIsNone(meta["proyecto"])
            self.assertIn("proyecto:\n", text)
            self.assertEqual(meta["carpeta"], "Bandeja de entrada")
            self.assertEqual(meta["hilo"], "conv-1")
            self.assertEqual(meta["adjuntos"], ["factura.pdf"])
            self.assertRegex(meta["fecha"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
            self.assertRegex(meta["ingestado"], r"^\d{4}-\d{2}-\d{2}T")
            self.assertNotIn("sintetico", text)
            self.assertIn("## Cuerpo\n\nTexto con acentos: información 😊", body)
            self.assertIn("- factura.pdf (application/pdf, 117 KB)", body)

            rule = expected_mail_path(env, server.messages[5], "minuta")
            rule_text = rule.read_text(encoding="utf-8")
            self.assertIn("Hola\n\\---\nlínea después de regla", rule_text)
            self.assertEqual(rule_text.count("\n---\n"), 1)  # only the frontmatter fence

            # the subfolder message keeps its display name
            other = expected_mail_path(env, server.messages[1], "plan-de-proyecto")
            self.assertEqual(parse_frontmatter(other.read_text(encoding="utf-8"))[0]["carpeta"], "Proyectos")
            # sync reads only; request shape per spec
            self.assertEqual(server.write_calls, [])
            message_requests = [p for m, p in server.paths if p.startswith("/me/messages?")]
            self.assertTrue(message_requests)
            self.assertIn("receivedDateTime%20asc", message_requests[0])
            self.assertIn('outlook.body-content-type="text", IdType="ImmutableId"', server.prefers)
            self.assertTrue(any("/attachments?$select=name,contentType,size" in p for _, p in server.paths))
            lines = env.log_lines()
            self.assertEqual(len(lines), 1)
            self.assertRegex(lines[0], r"^\S+ \| acme \| correo 3 nuevos \| calendario 16 días \| reuniones 0 \| estado ok$")
            self.assertNotIn("angela", lines[0].lower())
            self.assertFalse((env.vault / DEFAULT_LOCK).exists())

    def test_excluded_folders_are_not_written(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server("basic") as server:
            env = SyncEnv(tmp, server)
            self.assertEqual(env.sync("--profile", "acme").returncode, 0)
            written = {parse_frontmatter(p.read_text(encoding="utf-8"))[0]["id_origen"]
                       for p in md_files(env.mail_dir())}
            self.assertEqual(written, {"AAMk-msg-1/x+y==", "AAMk-msg-2", "AAMk-msg-3"})
            for excluded in ("AAMk-junk", "AAMk-draft", "AAMk-deleted"):
                self.assertNotIn(excluded, written)

    def test_second_run_is_immutable_and_advances_watermark(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server("basic") as server:
            env = SyncEnv(tmp, server)
            self.assertEqual(env.sync("--profile", "acme").returncode, 0)
            before = snapshot(env.mail_dir())
            state = json.loads((env.config / "acme" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["correo"]["marca"], server.messages[-1]["receivedDateTime"])
            index = (env.config / "acme" / "ids-correo.txt").read_text(encoding="utf-8").split()
            self.assertEqual(sorted(index), sorted(["AAMk-msg-1/x+y==", "AAMk-msg-2", "AAMk-msg-3"]))

            # A changed subject upstream must not rewrite the immutable file.
            server.messages[0]["subject"] = "cambiado"
            server.paths.clear()
            second = env.sync("--profile", "acme")
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(snapshot(env.mail_dir()), before)
            query = next(p for _, p in server.paths if p.startswith("/me/messages?"))
            overlap = (sync_cerebro.parse_graph_datetime(state["correo"]["marca"]) - timedelta(minutes=10))
            self.assertIn(sync_cerebro.iso_z(overlap), query.replace("%3A", ":"))  # re-reads from mark - 10 min
            self.assertIn("correo 0 nuevos", env.log_lines()[-1])
            self.assertEqual(len((env.config / "acme" / "ids-correo.txt").read_text(encoding="utf-8").split()), 3)

    def test_name_collision_gets_sha6_suffix_and_crash_recovery(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server("basic") as server:
            when = local_at(-1, 7, 30)
            server.messages = [
                make_message("dup-a", "Mismo asunto", when),
                make_message("dup-b", "Mismo asunto", when),
            ]
            env = SyncEnv(tmp, server)
            self.assertEqual(env.sync("--profile", "acme").returncode, 0)
            day = env.mail_dir() / when.strftime("%Y-%m-%d")
            names = sorted(p.name for p in day.glob("*.md"))
            suffix = sync_cerebro.sha6("dup-b")
            self.assertEqual(names, sorted([f"{when:%H%M}-mismo-asunto.md", f"{when:%H%M}-mismo-asunto-{suffix}.md"]))
            # crash between file write and index append: the next run repairs the index, no duplicate
            index = env.config / "acme" / "ids-correo.txt"
            index.write_text("dup-a\n", encoding="utf-8")
            (env.config / "acme" / "state.json").unlink()
            self.assertEqual(env.sync("--profile", "acme").returncode, 0)
            self.assertEqual(sorted(p.name for p in day.glob("*.md")), names)
            self.assertEqual(sorted(index.read_text(encoding="utf-8").split()), ["dup-a", "dup-b"])

    def test_max_messages_per_run_cuts_warns_and_continues(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server("basic") as server:
            server.messages = [
                make_message(f"m-{i}", f"Mensaje {i}", local_at(-3, 8, i)) for i in range(5)
            ]
            env = SyncEnv(tmp, server)
            run_fetch(env.env, "configure", "--profile", "acme", "--max-messages-per-run", "2")
            first = env.sync("--profile", "acme")
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(len(md_files(env.mail_dir())), 2)
            self.assertIn("tope de 2 mensajes alcanzado, continúa en la siguiente", env.log_lines()[-1])
            for _ in range(3):
                self.assertEqual(env.sync("--profile", "acme").returncode, 0)
            ids = sorted(parse_frontmatter(p.read_text(encoding="utf-8"))[0]["id_origen"]
                         for p in md_files(env.mail_dir()))
            self.assertEqual(ids, [f"m-{i}" for i in range(5)])
            self.assertNotIn("tope", env.log_lines()[-1])

    def test_calendar_day_files_cancellation_deletion_and_empty_days(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server("basic") as server:
            env = SyncEnv(tmp, server)
            self.assertEqual(env.sync("--profile", "acme", "--only", "calendar").returncode, 0)
            cal = env.vault / DEFAULT_RAW / "calendario" / "acme"
            files = sorted(p.name for p in cal.glob("*.md"))
            today = datetime.now().astimezone().date()
            expected = [(today + timedelta(days=d)).isoformat() + ".md" for d in range(-1, 15)]
            self.assertEqual(files, expected)
            day0 = cal / f"{today.isoformat()}.md"
            meta, body = parse_frontmatter(day0.read_text(encoding="utf-8"))
            self.assertEqual(meta["fuente"], "calendario")
            self.assertEqual(meta["empresa"], "acme")
            self.assertEqual(meta["id_origen"], ["EV-1"])
            self.assertEqual(meta["asunto"], f"Agenda {today.isoformat()}")
            self.assertEqual(meta["participantes"], [
                "Ángela Pérez <angela@example.invalid>", "Luis Gómez <luis@example.invalid>",
            ])
            self.assertIn(
                "## 09:00–10:00 · Comité: revisión\n"
                "- id_origen: EV-1\n"
                "- organizador: Ángela Pérez <angela@example.invalid>\n"
                "- participantes: Ángela Pérez <angela@example.invalid>; Luis Gómez <luis@example.invalid>\n"
                "- lugar: Sala 1; Sala Norte\n"
                "- teams: no\n",
                body,
            )
            cancelled = (cal / f"{(today + timedelta(days=1)).isoformat()}.md").read_text(encoding="utf-8")
            self.assertIn("- estado: cancelado", cancelled)
            for offset in (2, 3):  # all-day event spans two local days
                text = (cal / f"{(today + timedelta(days=offset)).isoformat()}.md").read_text(encoding="utf-8")
                self.assertIn("## Todo el día · Offsite", text)
            empty = parse_frontmatter((cal / f"{(today + timedelta(days=5)).isoformat()}.md").read_text(encoding="utf-8"))
            self.assertEqual(empty[0]["eventos"], "0")
            self.assertEqual(empty[0]["id_origen"], [])

            untouched = cal / f"{(today + timedelta(days=5)).isoformat()}.md"
            untouched_bytes = untouched.read_bytes()
            time.sleep(1.1)  # the next run has a different `ingestado`
            server.events = [e for e in server.events if e["id"] != "EV-1"]
            self.assertEqual(env.sync("--profile", "acme", "--only", "calendar").returncode, 0)
            meta, body = parse_frontmatter(day0.read_text(encoding="utf-8"))
            self.assertEqual(meta["id_origen"], [])
            self.assertNotIn("EV-1", body)
            self.assertIn("Sin eventos.", body)
            # unchanged days keep their bytes (hash-based consumers see no novelty)
            self.assertEqual(untouched.read_bytes(), untouched_bytes)

    def test_meetings_write_md_and_vtt_and_403_is_logged_without_stopping(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server("basic") as server:
            join = "https://teams.microsoft.com/l/meetup-join/19%3ameeting_x%40thread.v2/0?context=%7b%7d"
            start, end = local_at(-1, 15, 0), local_at(-1, 16, 0)
            server.events.append(make_event(
                "EV-T", "Revisión de proyecto Q3", start, end, teams_join=join,
                organizer=("Ana Pérez", "ana@example.invalid"),
                attendees=[attendee("Luis Gómez", "luis@example.invalid"),
                           attendee("Sala Teams", "sala@example.invalid", "resource")],
            ))
            server.online_meetings[join] = "MEET-1"
            server.transcripts["MEET-1"] = [{"id": "TR-1", "createdDateTime": fake_graph.iso_z(start)}]
            server.transcript_content["TR-1"] = VTT
            env = SyncEnv(tmp, server)
            run_fetch(env.env, "configure", "--profile", "acme", "--teams")
            result = env.sync("--profile", "acme")
            self.assertEqual(result.returncode, 0, result.stderr)
            meetings = env.vault / DEFAULT_RAW / "reuniones" / "acme"
            md = meetings / f"{start:%Y-%m-%d}-revision-de-proyecto-q3.md"
            vtt = md.with_suffix(".vtt")
            self.assertTrue(md.is_file() and vtt.is_file(), sorted(os.listdir(meetings)))
            self.assertEqual(vtt.read_text(encoding="utf-8"), VTT)
            meta, body = parse_frontmatter(md.read_text(encoding="utf-8"))
            self.assertEqual(meta["fuente"], "reunion")
            self.assertEqual(meta["id_origen"], "TR-1")
            self.assertEqual(meta["organizador"], "Ana Pérez <ana@example.invalid>")
            self.assertEqual(meta["participantes"], ["Ana Pérez <ana@example.invalid>", "Luis Gómez <luis@example.invalid>"])
            self.assertEqual(meta["vtt"], vtt.name)
            self.assertIn("[00:00:05] Ana Pérez: Hola a todos, arrancamos.", body)
            self.assertIn("[00:02:00] Desconocido: Sin hablante identificado", body)
            self.assertIn("reuniones 1", env.log_lines()[-1])
            self.assertEqual((env.config / "acme" / "ids-reuniones.txt").read_text(encoding="utf-8").split(), ["TR-1"])
            # immutable: second run does not rewrite it
            before = snapshot(meetings)
            self.assertEqual(env.sync("--profile", "acme").returncode, 0)
            self.assertEqual(snapshot(meetings), before)

            # tenant switch off -> 403 with innerError code, mail and calendar keep going
            server.transcripts["MEET-1"].append({"id": "TR-2", "createdDateTime": fake_graph.iso_z(start)})
            server.transcripts_forbidden = True
            server.messages.append(make_message("AAMk-new", "Nuevo tras 403", datetime.now().astimezone() - timedelta(minutes=5)))
            forbidden = env.sync("--profile", "acme")
            self.assertEqual(forbidden.returncode, 0, forbidden.stderr)
            line = env.log_lines()[-1]
            self.assertIn("reuniones 0 (sin acceso: 403 GraphAccessToTranscriptsDisabled)", line)
            self.assertIn("correo 1 nuevos", line)
            self.assertIn("calendario 16 días", line)
            self.assertIn("estado ok", line)

    def test_meetings_without_speaker_attribution_fall_back(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server("basic") as server:
            join = "https://teams.microsoft.com/l/meetup-join/abc"
            start, end = local_at(-1, 11, 0), local_at(-1, 11, 45)
            server.events = [make_event("EV-T", "Sin atribución", start, end, teams_join=join)]
            server.messages = []
            server.online_meetings[join] = "MEET-2"
            server.transcripts["MEET-2"] = [{"id": "TR-9", "createdDateTime": fake_graph.iso_z(start)}]
            server.transcript_content["TR-9"] = "0:0:5.0 --> 0:0:8.0\nHola a todos\n"
            server.speaker_attribution_off = True
            env = SyncEnv(tmp, server)
            run_fetch(env.env, "configure", "--profile", "acme", "--teams")
            self.assertEqual(env.sync("--profile", "acme").returncode, 0)
            md = env.vault / DEFAULT_RAW / "reuniones" / "acme" / f"{start:%Y-%m-%d}-sin-atribucion.md"
            text = md.read_text(encoding="utf-8")
            self.assertIn("hablantes: sin-atribucion", text)
            self.assertIn("[00:00:05] Desconocido: Hola a todos", text)
            self.assertIn("application/vnd.microsoft.graph.transcript+text", server.accepts)

    def test_calendar_failure_does_not_cost_the_mail(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server("basic") as server:
            server.fail_paths["/me/calendarView"] = 403
            env = SyncEnv(tmp, server)
            result = env.sync("--profile", "acme")
            self.assertEqual(result.returncode, 1)
            self.assertEqual(len(md_files(env.mail_dir())), 3)
            line = env.log_lines()[-1]
            self.assertIn("correo 3 nuevos", line)
            self.assertIn("estado error: calendario Graph 403", line)
            self.assertFalse((env.vault / DEFAULT_LOCK).exists())

    def test_meetings_skipped_silently_without_teams(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server("basic") as server:
            env = SyncEnv(tmp, server)
            self.assertEqual(env.sync("--profile", "acme").returncode, 0)
            self.assertFalse(any("/onlineMeetings" in p for _, p in server.paths))
            self.assertFalse((env.vault / DEFAULT_RAW / "reuniones").exists())

    def test_failed_refresh_exits_4_sentinel_log_and_never_opens_browser(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server("basic") as server:
            env = SyncEnv(tmp, server)
            server.token_fail = True
            module = load_fetch_module(env.config)
            with mock.patch.dict(os.environ, env.env, clear=True), \
                    mock.patch.object(module.webbrowser, "open", side_effect=AssertionError("browser opened")) as browser, \
                    mock.patch.object(sys, "argv", ["fetch.py", "sync", "--profile", "acme"]), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = module.main()
            self.assertEqual(code, 4)
            browser.assert_not_called()
            sentinel = env.config / "acme" / "needs-login"
            self.assertTrue(sentinel.is_file())
            self.assertIn("AADSTS70008", sentinel.read_text(encoding="utf-8"))
            line = env.log_lines()[-1]
            self.assertIn("estado requiere-login", line)
            self.assertIn("fix --profile acme", line)
            self.assertEqual(md_files(env.vault / DEFAULT_RAW), [])

            # no token at all: still exit 4, still no browser (the OAuth flow itself refuses)
            (env.config / "acme" / "token.json").unlink()
            module = load_fetch_module(env.config)
            with mock.patch.dict(os.environ, env.env, clear=True), \
                    mock.patch.object(module.webbrowser, "open", side_effect=AssertionError("browser opened")) as browser, \
                    mock.patch.object(sys, "argv", ["fetch.py", "--profile", "acme", "sync"]), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(module.main(), 4)
                module._non_interactive = True
                with self.assertRaises(sync_cerebro.NeedsLogin):
                    module._run_oauth_flow(module.load_config(require_client=False))
            browser.assert_not_called()

            # `fix --profile acme` (a person signs in) clears the sentinel
            module = load_fetch_module(env.config)
            with mock.patch.dict(os.environ, env.env, clear=True), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                module._select_profile("acme")
                fresh = {"access_token": "a", "refresh_token": "r", "expires_in": 3600, "expires_at": time.time() + 3600}
                with mock.patch.object(module, "_run_oauth_flow", return_value=fresh):
                    ok, _msg = module._fix_full_reauth()
            self.assertTrue(ok)
            self.assertFalse(sentinel.exists())

    def test_sync_all_one_lock_isolated_failures_and_empresa_everywhere(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server("basic") as server:
            env = SyncEnv(tmp, server, profiles=("globex", "acme"))
            seen: list[str] = []
            lock_file = env.vault / DEFAULT_LOCK

            def observe(path: str) -> None:
                if path.startswith("/me/messages") or path == "/me/calendarView":
                    try:
                        seen.append(lock_file.read_text(encoding="utf-8"))
                    except OSError:
                        seen.append("<sin lock>")

            server.on_request = observe
            result = env.sync("--all")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(seen)
            self.assertEqual(len(set(seen)), 1, "both profiles must run under ONE lock acquisition")
            held = json.loads(seen[0])
            self.assertEqual(held["paso"], "ingesta")
            self.assertEqual(held["perfil"], "acme,globex")
            self.assertFalse(lock_file.exists())
            lines = env.log_lines()
            self.assertEqual([l.split(" | ")[1] for l in lines], ["acme", "globex"])  # alphabetical
            self.assertTrue(all(l.endswith("estado ok") for l in lines))

            # every raw .md carries a non-empty empresa that matches its folder
            raw = env.vault / DEFAULT_RAW
            files = md_files(raw)
            self.assertGreater(len(files), 30)
            for path in files:
                meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
                folder_empresa = path.relative_to(raw).parts[1]
                self.assertTrue(meta.get("empresa"), path)
                self.assertEqual(meta["empresa"], folder_empresa, path)
                self.assertNotIn("sintetico", meta)

            # a profile that needs login does not block the other; worst exit code wins
            (env.config / "globex" / "token.json").unlink()
            server.messages.append(make_message("AAMk-late", "Tarde", datetime.now().astimezone() - timedelta(minutes=1)))
            mixed = env.sync("--all")
            self.assertEqual(mixed.returncode, 4, mixed.stdout + mixed.stderr)
            acme_line, globex_line = env.log_lines()[-2:]
            self.assertIn("acme | correo 1 nuevos", acme_line)
            self.assertIn("estado ok", acme_line)
            self.assertIn("globex", globex_line)
            self.assertIn("requiere-login", globex_line)
            self.assertFalse(lock_file.exists())

    def test_time_budget_cuts_cleanly_and_next_run_continues(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server("basic") as server:
            server.messages = [
                make_message(f"slow-{i}", f"Lento {i}", local_at(-2, 7, i), has_attachments=True) for i in range(6)
            ]
            server.delays["/me/messages/"] = 0.35
            env = SyncEnv(tmp, server, profiles=("acme", "globex"))
            budget_env = dict(env.env, INGEST_OUTLOOK_SYNC_BUDGET_SECONDS="2.5")
            first = env.sync("--all", env=budget_env)
            self.assertEqual(first.returncode, 0, first.stderr)
            lines = env.log_lines()
            self.assertTrue(any("tope de tiempo alcanzado, continúa en la siguiente" in l for l in lines), lines)
            self.assertFalse((env.vault / DEFAULT_LOCK).exists())
            written_first = len(md_files(env.mail_dir("acme"))) + len(md_files(env.mail_dir("globex")))
            self.assertLess(written_first, 12)
            server.delays.clear()
            self.assertEqual(env.sync("--all").returncode, 0)
            for empresa in ("acme", "globex"):
                ids = sorted(parse_frontmatter(p.read_text(encoding="utf-8"))[0]["id_origen"]
                             for p in md_files(env.mail_dir(empresa)))
                self.assertEqual(ids, [f"slow-{i}" for i in range(6)], empresa)

    def test_two_concurrent_syncs_on_one_vault_both_finish(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server("basic") as server:
            env = SyncEnv(tmp, server, profiles=("acme", "globex"))
            procs = [
                subprocess.Popen(
                    [sys.executable, str(FETCH), "sync", "--profile", profile],
                    env=env.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                for profile in ("acme", "globex")
            ]
            outputs = [p.communicate(timeout=120) for p in procs]
            for proc, (out, err) in zip(procs, outputs):
                self.assertEqual(proc.returncode, 0, err.decode("utf-8", "replace"))
            lines = env.log_lines()
            self.assertEqual(len(lines), 2)
            self.assertTrue(all("estado ok" in l for l in lines), lines)
            self.assertFalse((env.vault / DEFAULT_LOCK).exists())
            for path in md_files(env.vault / DEFAULT_RAW):
                parse_frontmatter(path.read_text(encoding="utf-8"))  # parses = not corrupt
            self.assertEqual(len(md_files(env.mail_dir("acme"))), 3)
            self.assertEqual(len(md_files(env.mail_dir("globex"))), 3)

    def test_abandoned_lock_is_stolen_and_busy_lock_skips(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server("basic") as server:
            env = SyncEnv(tmp, server)
            lock_file = env.vault / DEFAULT_LOCK
            lock_file.parent.mkdir(parents=True, exist_ok=True)
            old = (datetime.now().astimezone() - timedelta(minutes=25)).isoformat(timespec="seconds")
            lock_file.write_text(json.dumps({"paso": "extraccion", "pid": 999999, "inicio": old,
                                             "equipo": "otro-pc"}), encoding="utf-8")
            result = env.sync("--profile", "acme")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("lock abandonado robado (paso extraccion", env.log_lines()[-1])
            self.assertFalse(lock_file.exists())

            fresh = datetime.now().astimezone().isoformat(timespec="seconds")
            lock_file.write_text(json.dumps({"paso": "s3", "pid": 1, "inicio": fresh, "equipo": "otro-pc"}),
                                 encoding="utf-8")
            before = snapshot(env.vault / DEFAULT_RAW)
            busy = env.sync("--profile", "acme", env=dict(env.env, CEREBRO_LOCK_WAIT_SECONDS="0.5"))
            self.assertEqual(busy.returncode, 0, busy.stderr)
            self.assertRegex(env.log_lines()[-1], r"\| acme \| correo 0 nuevos .* estado saltada: lock ocupado por s3$")
            self.assertEqual(snapshot(env.vault / DEFAULT_RAW), before)
            self.assertTrue(lock_file.exists())  # somebody else's lock stays

    def test_lock_released_after_exception(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server("basic") as server:
            env = SyncEnv(tmp, server)
            module = load_fetch_module(env.config)
            args = argparse.Namespace(all=False, only=None, dry_run=False, vault_root=None, _profile_explicit=True)
            with mock.patch.dict(os.environ, env.env, clear=True), \
                    mock.patch.object(module, "_sync_one", side_effect=RuntimeError("boom")), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                module._select_profile("acme")
                with self.assertRaises(RuntimeError):
                    module.cmd_sync(args)
            self.assertFalse((env.vault / DEFAULT_LOCK).exists())

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server("basic") as server:
            env = SyncEnv(tmp, server)
            seed_token(env.config / "acme", expired=False)
            vault_before = snapshot(env.vault)
            profile_before = snapshot(env.config / "acme")
            result = env.sync("--profile", "acme", "--dry-run")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(snapshot(env.vault), vault_before)
            after = snapshot(env.config / "acme")
            after.pop("log.jsonl", None)
            profile_before.pop("log.jsonl", None)
            self.assertEqual(after, profile_before)  # no state, no index, no cuenta cache
            self.assertIn("[dry-run] correo: raw/entradas/correo/acme/", result.stdout)
            self.assertIn("[dry-run] calendario:", result.stdout)
            self.assertIn("[dry-run] ", result.stdout.splitlines()[-1])

    def test_custom_paths_registros_dir_and_canonical_defaults(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server("basic") as server:
            env = SyncEnv(tmp, server, profiles=())
            configure_profile(env.env, "acme", env.vault, "--raw-root", "00-entrada",
                              "--registros-dir", "30-registros")
            seed_token(env.config / "acme")
            show = run_fetch(env.env, "configure", "--show", "--profile", "acme")
            self.assertIn('lock_path: "30-registros/.cerebro.lock" (source: file)', show.stdout)
            self.assertIn('ingest_log_path: "30-registros/ingesta.log" (source: file)', show.stdout)
            self.assertEqual(env.sync("--all").returncode, 0)
            self.assertTrue((env.vault / "00-entrada" / "correo" / "acme").is_dir())
            self.assertEqual(len(env.log_lines(Path("30-registros") / "ingesta.log")), 1)
            self.assertFalse((env.vault / "raw").exists())
            self.assertFalse((env.vault / ".claude").exists())
            for bad in ("../fuera", "C:\\x", "/abs", "a/con/b"):
                self.assertEqual(run_fetch(env.env, "configure", "--profile", "acme", "--raw-root", bad).returncode, 2, bad)
            # explicit flags win over --registros-dir
            run_fetch(env.env, "configure", "--profile", "acme", "--registros-dir", "logs",
                      "--lock-path", ".claude/system3/cerebro.lock")
            show = run_fetch(env.env, "configure", "--show", "--profile", "acme")
            self.assertIn('lock_path: ".claude/system3/cerebro.lock"', show.stdout)
            self.assertIn('ingest_log_path: "logs/ingesta.log"', show.stdout)

    def test_single_lock_per_vault_is_enforced(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server("basic") as server:
            env = SyncEnv(tmp, server, profiles=("acme", "globex"))
            run_fetch(env.env, "configure", "--profile", "globex", "--lock-path", "otra/ruta.lock")
            refused = env.sync("--all")
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("lock_path", refused.stderr)
            self.assertEqual(md_files(env.vault), [])
            single = env.sync("--profile", "acme")
            self.assertNotEqual(single.returncode, 0)

            run_fetch(env.env, "configure", "--profile", "globex", "--lock-path", ".claude/system3/cerebro.lock")
            system3 = env.vault / ".claude" / "system3" / "config.json"
            system3.parent.mkdir(parents=True, exist_ok=True)
            system3.write_text(json.dumps({"lock_path": "30-registros/.cerebro.lock"}), encoding="utf-8")
            mismatch = env.sync("--all")
            self.assertNotEqual(mismatch.returncode, 0)
            self.assertIn(".claude/system3/config.json", mismatch.stderr)
            system3.write_text(json.dumps({"lock_path": ".claude/system3/cerebro.lock"}), encoding="utf-8")
            self.assertEqual(env.sync("--all").returncode, 0)

    def test_profiles_separate_state_and_show_sources(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server("basic") as server:
            env = SyncEnv(tmp, server, profiles=("acme", "globex"))
            show = run_fetch(env.env, "configure", "--show", "--profile", "acme")
            self.assertEqual(show.returncode, 0, show.stderr)
            self.assertIn("Profile: acme", show.stdout)
            self.assertIn(str(env.config / "acme" / "config.json"), show.stdout)
            self.assertIn('empresa: "acme" (source: file)', show.stdout)
            self.assertIn('layout: "cerebro" (source: file)', show.stdout)
            self.assertIn('raw_root: "raw/entradas" (source: default)', show.stdout)
            self.assertIn('lock_path: ".claude/system3/cerebro.lock" (source: default)', show.stdout)
            self.assertIn('ingest_log_path: ".claude/system3/logs/ingesta.log" (source: default)', show.stdout)
            self.assertIn("backfill_days: 30 (source: default)", show.stdout)
            self.assertIn('mail_exclude_folders: ["junkemail", "deleteditems", "drafts"] (source: default)', show.stdout)
            # --profile after the subcommand and INGEST_OUTLOOK_PROFILE are equivalent
            by_env = run_fetch(dict(env.env, INGEST_OUTLOOK_PROFILE="globex"), "configure", "--show")
            self.assertIn('empresa: "globex" (source: file)', by_env.stdout)

            self.assertEqual(run_fetch(dict(env.env, INGEST_OUTLOOK_PROFILE="acme"), "sync").returncode, 0)
            self.assertTrue((env.config / "acme" / "state.json").is_file())
            self.assertTrue((env.config / "acme" / "ids-correo.txt").is_file())
            self.assertFalse((env.config / "globex" / "state.json").exists())
            self.assertFalse((env.config / "globex" / "ids-correo.txt").exists())
            self.assertEqual(json.loads((env.config / "acme" / "token.json").read_text(encoding="utf-8"))["access_token"],
                             "refreshed-access")
            self.assertEqual(json.loads((env.config / "globex" / "token.json").read_text(encoding="utf-8"))["access_token"],
                             "seed-access")
            # the base (no profile) config is untouched: v0.5.1 layout stays the default
            self.assertFalse((env.config / "config.json").exists())
            self.assertEqual(run_fetch(env.env, "configure", "--profile", "BAD SLUG", "--show").returncode, 2)

    def test_empresa_is_mandatory_for_cerebro(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server("basic") as server:
            env = SyncEnv(tmp, server, profiles=())
            missing = run_fetch(env.env, "configure", "--profile", "acme", "--layout", "cerebro",
                                "--client-id", CLIENT_ID, "--tenant-id", TENANT_ID, "--vault-root", str(env.vault))
            self.assertEqual(missing.returncode, 2)
            self.assertIn("--empresa", missing.stderr)
            self.assertFalse((env.config / "acme" / "config.json").exists())
            self.assertEqual(run_fetch(env.env, "configure", "--profile", "acme", "--empresa", "Acme SAS").returncode, 2)

            # a hand-edited profile without empresa: sync refuses, logs it, doctor fails
            profile_dir = env.config / "acme"
            profile_dir.mkdir(parents=True, exist_ok=True)
            (profile_dir / "config.json").write_text(json.dumps({
                "client_id": CLIENT_ID, "tenant_id": TENANT_ID, "read_only": True,
                "vault_root": str(env.vault), "layout": "cerebro",
            }), encoding="utf-8")
            seed_token(profile_dir)
            refused = env.sync("--profile", "acme")
            self.assertEqual(refused.returncode, 2)
            self.assertIn("empresa", refused.stderr)
            self.assertIn("estado error: falta empresa", env.log_lines()[-1])
            self.assertFalse((env.vault / DEFAULT_RAW).exists())
            doctor = run_fetch(env.env, "doctor", "--profile", "acme")
            self.assertIn("[FAIL] empresa", doctor.stdout)

            # an invalid empresa (not a slug) is refused the same way, with a log line
            config = json.loads((profile_dir / "config.json").read_text(encoding="utf-8"))
            config["empresa"] = "Acme SAS"
            (profile_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
            invalid = env.sync("--all")
            self.assertEqual(invalid.returncode, 2)
            self.assertIn("estado error: configuración inválida (empresa must be a slug", env.log_lines()[-1])
            self.assertFalse((env.vault / DEFAULT_RAW).exists())

    def test_legacy_layout_refuses_sync_and_keeps_v05_behavior(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server("basic") as server:
            env = SyncEnv(tmp, server, profiles=())
            base = env.config
            base.mkdir(parents=True, exist_ok=True)
            (base / "config.json").write_text(json.dumps({
                "client_id": CLIENT_ID, "tenant_id": TENANT_ID, "read_only": True, "vault_root": str(env.vault),
            }), encoding="utf-8")
            seed_token(base)
            result = env.sync()
            self.assertEqual(result.returncode, 2)
            self.assertIn("layout cerebro", result.stderr)
            show = run_fetch(env.env, "configure", "--show")
            self.assertIn('layout: "legacy" (source: default)', show.stdout)
            self.assertIn('output_dir: "External Inputs/Outlook" (source: default)', show.stdout)

    def test_doctor_reports_needs_login_and_fix_clears_when_session_works(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server("basic") as server:
            env = SyncEnv(tmp, server)
            (env.config / "acme" / "needs-login").write_text("{}", encoding="utf-8")
            doctor = run_fetch(env.env, "doctor", "--profile", "acme")
            self.assertIn("[WARN] needs-login", doctor.stdout)
            self.assertIn("[PASS] vault-lock", doctor.stdout)
            fixed = run_fetch(env.env, "fix", "--profile", "acme")
            self.assertEqual(fixed.returncode, 0, fixed.stdout + fixed.stderr)
            self.assertFalse((env.config / "acme" / "needs-login").exists())


# ---------------------------------------------------------------------------
# schedule
# ---------------------------------------------------------------------------

class ScheduleTests(unittest.TestCase):
    """Nothing here registers a real task or loads a real LaunchAgent: the
    platform seams and the PowerShell/launchctl runners are patched."""

    def _args(self, action: str, vault: str | None, dry_run: bool = False) -> argparse.Namespace:
        return argparse.Namespace(action=action, vault_root=vault, every=30, start="07:00", hours=12,
                                  dry_run=dry_run)

    def test_schedule_on_linux_exits_0_with_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config"
            module = load_fetch_module(config)
            with mock.patch.dict(os.environ, clean_env(config), clear=True), \
                    mock.patch.object(module, "_is_windows", return_value=False), \
                    mock.patch.object(module, "_is_macos", return_value=False), \
                    mock.patch.object(module.schedule_win, "run_powershell", side_effect=AssertionError), \
                    mock.patch.object(module.schedule_mac, "run_launchctl", side_effect=AssertionError):
                for action in ("install", "status", "remove"):
                    with redirect_stdout(io.StringIO()) as out:
                        self.assertEqual(module.cmd_schedule(self._args(action, tmp)), 0)
                    self.assertIn("en este sistema no se programó nada", out.getvalue())

    def test_schedule_install_builds_register_command_via_seam(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config"
            module = load_fetch_module(config)
            captured: list[str] = []

            def fake_run(script: str, timeout: int = 120):
                captured.append(script)
                return 0, "REGISTERED|\\Rewired\\Cerebro - ingesta\n", ""

            args = self._args("install", str(Path(tmp) / "vault con tildé"))
            with mock.patch.dict(os.environ, clean_env(config), clear=True), \
                    mock.patch.object(module, "_is_windows", return_value=True), \
                    mock.patch.object(module.schedule_win, "run_powershell", side_effect=fake_run), \
                    redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
                module._select_profile(None)
                self.assertEqual(module.cmd_schedule(args), 0)
            self.assertIn("\\Rewired\\Cerebro - ingesta", out.getvalue())
            script = captured[0]
            self.assertIn("Register-ScheduledTask", script)
            self.assertIn("sync --all --vault-root", script)
            self.assertIn(str(FETCH.resolve()), script)

            def denied(script: str, timeout: int = 120):
                return 5, "DENIED|\\Rewired\\|Access is denied.\n", ""

            with mock.patch.dict(os.environ, clean_env(config), clear=True), \
                    mock.patch.object(module, "_is_windows", return_value=True), \
                    mock.patch.object(module.schedule_win, "run_powershell", side_effect=denied), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                self.assertEqual(module.cmd_schedule(args), 5)
            self.assertIn("negó el acceso", err.getvalue())
            self.assertIn("respaldo-hook-inicio.md", err.getvalue())

    def test_macos_plist_content(self):
        vault = "/Users/ana/cerebros/gerencia comercial"
        content = schedule_mac.build_plist(
            python="/usr/local/bin/python3", fetch_path="/opt/ingest-outlook/fetch.py", vault=vault,
            every_minutes=30, log_path="/tmp/launchd.log", environment={"INGEST_OUTLOOK_CONFIG_DIR": "/x"},
        )
        text = content.decode("utf-8")
        self.assertIn("<key>StartInterval</key>\n\t<integer>1800</integer>", text)
        self.assertIn(f"<string>{vault}</string>", text)
        try:
            import plistlib
        except ImportError as exc:  # e.g. a Python build whose pyexpat is broken
            self.skipTest(f"plistlib unavailable to round-trip the plist: {exc}")
        data = plistlib.loads(content)
        label = schedule_mac.label_for(vault)
        self.assertRegex(label, r"^com\.cerebro\.ingesta\.[0-9a-f]{8}$")
        self.assertEqual(data["Label"], label)
        self.assertEqual(data["ProgramArguments"], [
            "/usr/local/bin/python3", "/opt/ingest-outlook/fetch.py", "sync", "--all", "--vault-root", vault,
        ])
        self.assertEqual(data["StartInterval"], 1800)
        self.assertFalse(data["RunAtLoad"])
        self.assertEqual(data["EnvironmentVariables"], {"INGEST_OUTLOOK_CONFIG_DIR": "/x"})
        self.assertEqual(schedule_mac.label_for(vault + "/"), label)  # same vault, same agent
        self.assertNotEqual(schedule_mac.label_for("/Users/ana/cerebros/otro"), label)
        with_profile = plistlib.loads(schedule_mac.build_plist("/py", "/f.py", vault, profile="acme"))
        tricky = plistlib.loads(schedule_mac.build_plist("/py", "/f.py", "/Users/a&b/<x> é"))
        self.assertEqual(tricky["ProgramArguments"][-1], "/Users/a&b/<x> é")
        self.assertEqual(with_profile["ProgramArguments"][2:5], ["--profile", "acme", "sync"])

    def test_macos_install_status_remove_via_seam(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config"
            agents = Path(tmp) / "LaunchAgents"
            vault = str(Path(tmp).resolve() / "cerebros" / "cargo")
            module = load_fetch_module(config)
            calls: list[list[str]] = []

            def fake_launchctl(args, timeout=30):
                calls.append(list(args))
                if args[0] == "print":
                    return 0, "\tstate = not running\n\truns = 3\n\tlast exit code = 0\n", ""
                return 0, "", ""

            env = dict(clean_env(config), INGEST_OUTLOOK_LAUNCHAGENTS_DIR=str(agents))
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(module, "_is_windows", return_value=False), \
                    mock.patch.object(module, "_is_macos", return_value=True), \
                    mock.patch.object(module.schedule_mac, "run_launchctl", side_effect=fake_launchctl), \
                    redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
                module._select_profile(None)
                self.assertEqual(module.cmd_schedule(self._args("install", vault)), 0)
                plist = agents / f"{schedule_mac.label_for(vault)}.plist"
                self.assertTrue(plist.is_file())
                self.assertEqual(module.cmd_schedule(self._args("status", vault)), 0)
                self.assertEqual(module.cmd_schedule(self._args("remove", vault)), 0)
                self.assertFalse(plist.exists())
            label = schedule_mac.label_for(vault)
            domain = schedule_mac.gui_domain()
            self.assertIn(["bootstrap", domain, str(plist)], calls)
            self.assertIn(["print", f"{domain}/{label}"], calls)
            self.assertIn(["bootout", f"{domain}/{label}"], calls)
            self.assertIn("Último resultado: 0", out.getvalue())

            def failing(args, timeout=30):
                return (5, "", "Bootstrap failed: 5: Input/output error") if args[0] == "bootstrap" else (0, "", "")

            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(module, "_is_windows", return_value=False), \
                    mock.patch.object(module, "_is_macos", return_value=True), \
                    mock.patch.object(module.schedule_mac, "run_launchctl", side_effect=failing), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                self.assertEqual(module.cmd_schedule(self._args("install", vault)), 1)
            self.assertIn("No se pudo activar la ingesta automática en macOS", err.getvalue())

    @unittest.skipUnless(os.name == "nt", "Windows only: real pythonw.exe resolution")
    def test_schedule_install_dry_run_on_windows_prints_register_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = clean_env(Path(tmp) / "config")
            result = run_fetch(env, "schedule", "install", "--dry-run", "--vault-root", str(Path(tmp) / "vault x"))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Register-ScheduledTask", result.stdout)
            self.assertIn("pythonw.exe", result.stdout.lower())
            self.assertIn("sync --all", result.stdout)
            self.assertIn("-LogonType Interactive", result.stdout)


if __name__ == "__main__":
    unittest.main()
