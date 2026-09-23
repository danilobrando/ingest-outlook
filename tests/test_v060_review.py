"""Regression tests for the adversarial review of v0.6 (one per finding).

Each test reproduces a defect found in review and fails on the code before
the fix. All of them run against the loopback fake Graph and temp dirs.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake_graph  # noqa: E402
from fake_graph import attendee, fake_server, local_at, make_event, make_message, seed_token  # noqa: E402
from support import LOCK_CLI, ROOT, run_fetch  # noqa: E402
from test_v060 import (  # noqa: E402
    DEFAULT_LOCK,
    DEFAULT_RAW,
    SyncEnv,
    configure_profile,
    md_files,
    parse_frontmatter,
)

sys.path.insert(0, str(ROOT))
import cerebro_lock  # noqa: E402
import schedule_win  # noqa: E402

VTT = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<v Ana>Hola</v>\n"


def mail_ids(env: SyncEnv, empresa: str = "acme") -> list[str]:
    folder = env.mail_dir(empresa)
    if not folder.exists():
        return []
    return sorted(parse_frontmatter(p.read_text(encoding="utf-8"))[0]["id_origen"] for p in md_files(folder))


def mail_meta(env: SyncEnv, message_id: str, empresa: str = "acme") -> dict:
    for path in md_files(env.mail_dir(empresa)):
        meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        if meta["id_origen"] == message_id:
            return meta
    raise AssertionError(f"{message_id} not written")


def inbox(server) -> None:
    server.folders["folder-inbox"] = {"id": "folder-inbox", "displayName": "Bandeja de entrada",
                                      "parentFolderId": "folder-root"}


def teams_env(tmp: str, server, *extra: str) -> SyncEnv:
    env = SyncEnv(tmp, server, profiles=())
    configure_profile(env.env, "acme", env.vault, "--teams", *extra)
    seed_token(env.config / "acme")
    return env


class MailStreamRegressions(unittest.TestCase):
    def test_p1_2_item_leaving_the_result_set_mid_run_does_not_hide_others(self):
        """$skip offsets shifted when a draft left the result set between
        pages; one message was skipped for good (keyset pagination fixes it)."""
        with tempfile.TemporaryDirectory() as tmp, fake_server() as server:
            inbox(server)
            server.events = []
            base = local_at(-3, 8, 0)
            server.messages = [make_message(f"m{i:03d}", f"asunto {i}", base + timedelta(minutes=i)) for i in range(60)]
            server.messages.append(make_message("draft-1", "borrador", base + timedelta(minutes=5, seconds=30),
                                                folder="folder-drafts"))
            state = {"lists": 0}
            original = server.RequestHandlerClass._messages

            def mutating(handler, rest, query):
                if not rest and "$search" not in query:
                    state["lists"] += 1
                    if state["lists"] == 2:  # the person sends the draft between pages
                        server.messages = [m for m in server.messages if m["id"] != "draft-1"]
                        server.messages.append(make_message("sent-1", "borrador", local_at(0, 0, 1),
                                                            folder="folder-sent"))
                return original(handler, rest, query)

            server.RequestHandlerClass._messages = mutating
            try:
                env = SyncEnv(tmp, server)
                for _ in range(2):
                    result = env.sync("--profile", "acme")
                    self.assertEqual(result.returncode, 0, result.stderr)
            finally:
                server.RequestHandlerClass._messages = original
            missing = sorted({f"m{i:03d}" for i in range(60)} - set(mail_ids(env)))
            self.assertEqual(missing, [])
            self.assertIn("sent-1", mail_ids(env))
            self.assertFalse(any("$skip" in path for _, path in server.paths if path.startswith("/me/messages?")))

    def test_p1_2_finding_11_same_timestamp_over_the_cap_does_not_livelock(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server() as server:
            inbox(server)
            server.events = []
            when = local_at(-1, 9, 0)
            server.messages = [make_message(f"s{i}", f"asunto {i}", when) for i in range(3)]
            server.messages.append(make_message("later", "despues", local_at(-1, 11, 0)))
            env = SyncEnv(tmp, server, profiles=())
            configure_profile(env.env, "acme", env.vault, "--max-messages-per-run", "2")
            seed_token(env.config / "acme")
            for _ in range(4):
                self.assertEqual(env.sync("--profile", "acme").returncode, 0)
            self.assertEqual(mail_ids(env), ["later", "s0", "s1", "s2"])
            self.assertNotIn("tope", env.log_lines()[-1])

    def test_p1_3_moving_a_message_keeps_its_immutable_id(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server() as server:
            inbox(server)
            server.folders["folder-proy"] = {"id": "folder-proy", "displayName": "Proyecto X",
                                             "parentFolderId": "folder-inbox"}
            server.events = []
            server.messages = [
                make_message("rest-A", "Primero", local_at(-1, 9, 0), immutable_id="imm-A"),
                make_message("rest-B", "Ultimo", local_at(-1, 10, 0), immutable_id="imm-B"),
            ]
            env = SyncEnv(tmp, server)
            self.assertEqual(env.sync("--profile", "acme").returncode, 0)
            # the person files the last message into a project folder: new REST id
            server.messages[1]["id"] = "rest-B-moved"
            server.messages[1]["parentFolderId"] = "folder-proy"
            self.assertEqual(env.sync("--profile", "acme").returncode, 0)
            self.assertEqual(mail_ids(env), ["imm-A", "imm-B"])
            gets = [path for method, path in server.paths if method == "GET"]
            mail_prefers = [pref for path, pref in zip(gets, server.prefers) if path.startswith("/me/messages")]
            self.assertTrue(mail_prefers)
            for pref in mail_prefers:
                self.assertEqual(pref, 'outlook.body-content-type="text", IdType="ImmutableId"')

    def test_p1_3_internet_message_id_is_recorded_and_a_second_key(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server() as server:
            inbox(server)
            server.events = []
            server.messages = [make_message("rest-1", "Hola", local_at(-1, 9, 0), immutable_id="imm-1",
                                            internet_id="<abc@mail.example.invalid>")]
            env = SyncEnv(tmp, server)
            self.assertEqual(env.sync("--profile", "acme").returncode, 0)
            self.assertEqual(mail_meta(env, "imm-1")["id_internet"], "<abc@mail.example.invalid>")
            # same message under another id (e.g. recorded before ImmutableId)
            server.messages[0]["_immutable"] = "imm-1-other-format"
            self.assertEqual(env.sync("--profile", "acme").returncode, 0)
            self.assertEqual(mail_ids(env), ["imm-1"])

    def test_p1_5_attachment_failure_does_not_stall_the_mailbox(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server() as server:
            inbox(server)
            server.events = []
            server.messages = [
                make_message("m1", "uno", local_at(-2, 9, 0)),
                make_message("m2", "dos", local_at(-2, 10, 0), has_attachments=True),
                make_message("m3", "tres", local_at(-1, 9, 0)),
                make_message("m4", "cuatro", local_at(-1, 10, 0)),
            ]
            server.fail_paths["/me/messages/m2/attachments"] = 503
            env = SyncEnv(tmp, server)
            result = env.sync("--profile", "acme")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(mail_ids(env), ["m1", "m2", "m3", "m4"])
            meta = mail_meta(env, "m2")
            self.assertEqual(meta["adjuntos"], [])
            self.assertEqual(meta["adjuntos_error"], "true")
            self.assertNotIn("adjuntos_error", mail_meta(env, "m1"))

    def test_p1_5_a_message_that_cannot_be_written_three_times_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server() as server:
            inbox(server)
            server.events = []
            poison_day = local_at(-2, 10, 0)
            server.messages = [
                make_message("p1", "antes", local_at(-3, 9, 0)),
                make_message("p2", "veneno", poison_day),
                make_message("p3", "despues", local_at(-1, 9, 0)),
            ]
            env = SyncEnv(tmp, server)
            blocker = env.mail_dir() / poison_day.strftime("%Y-%m-%d")
            blocker.parent.mkdir(parents=True, exist_ok=True)
            blocker.write_text("un archivo donde iría la carpeta del día", encoding="utf-8")
            for _ in range(3):
                env.sync("--profile", "acme")
            skipped = (env.config / "acme" / "skipped.txt").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(skipped), 1)
            self.assertTrue(skipped[0].startswith("p2\t"))
            self.assertIn("disco", skipped[0])
            state = json.loads((env.config / "acme" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["correo"]["marca"], server.messages[2]["receivedDateTime"])
            self.assertIn("omitidos", env.log_lines()[-1])
            self.assertEqual(mail_ids(env), ["p1", "p3"])
            fourth = env.sync("--profile", "acme")
            self.assertEqual(fourth.returncode, 0, fourth.stderr)  # skipped for good, not retried

    def test_finding_7_message_rescued_from_junk_is_written(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server() as server:
            inbox(server)
            server.events = []
            server.messages = [
                make_message("rest-j1", "cotizacion importante", local_at(-1, 9, 0), folder="folder-junk",
                             immutable_id="imm-j1"),
                make_message("rest-m2", "otro", local_at(-1, 10, 0), immutable_id="imm-m2"),
            ]
            env = SyncEnv(tmp, server)
            self.assertEqual(env.sync("--profile", "acme").returncode, 0)
            self.assertEqual(mail_ids(env), ["imm-m2"])
            server.messages[0]["id"] = "rest-j1-inbox"          # moved out of Junk
            server.messages[0]["parentFolderId"] = "folder-inbox"
            self.assertEqual(env.sync("--profile", "acme").returncode, 0)
            self.assertEqual(mail_ids(env), ["imm-j1", "imm-m2"])

    def test_finding_13_sender_first_falls_back_to_sender(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server() as server:
            inbox(server)
            server.events = []
            message = make_message("m1", "sin from", local_at(-1, 9, 0))
            message["from"] = {"emailAddress": {"name": "Buzón compartido", "address": ""}}
            message["sender"] = fake_graph.address("Gestor", "Gestor@Example.invalid")
            server.messages = [message]
            env = SyncEnv(tmp, server)
            self.assertEqual(env.sync("--profile", "acme").returncode, 0)
            self.assertEqual(mail_meta(env, "m1")["participantes"][0], "Gestor <gestor@example.invalid>")

    def test_finding_14_stale_temp_files_are_cleaned(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server("basic") as server:
            env = SyncEnv(tmp, server)
            day = env.mail_dir() / "2020-01-01"
            day.mkdir(parents=True)
            old = day / ".0915-asunto.md.4242.tmp"
            fresh = day / ".0916-asunto.md.4343.tmp"
            other = day / "notas.tmp"
            for path in (old, fresh, other):
                path.write_text("x", encoding="utf-8")
            an_hour_ago = time.time() - 3600
            os.utime(old, (an_hour_ago, an_hour_ago))
            os.utime(other, (an_hour_ago, an_hour_ago))
            self.assertEqual(env.sync("--profile", "acme").returncode, 0)
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(other.exists())

    def test_finding_16_empresa_is_quoted_in_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server("basic") as server:
            env = SyncEnv(tmp, server, profiles=())
            configure_profile(env.env, "acme", env.vault, empresa="on")  # YAML 1.1 would read `on` as true
            seed_token(env.config / "acme")
            self.assertEqual(env.sync("--profile", "acme").returncode, 0)
            files = md_files(env.vault / DEFAULT_RAW)
            self.assertTrue(files)
            for path in files:
                text = path.read_text(encoding="utf-8")
                self.assertIn('\nempresa: "on"\n', text, path)


class StageRegressions(unittest.TestCase):
    def test_p0_1_transcript_older_than_yesterday_is_fetched(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server() as server:
            inbox(server)
            server.messages = []
            join = "https://teams.microsoft.com/l/meetup-join/19%3ameeting_x%40thread.v2/0?context=%7b%7d"
            start = local_at(-3, 16, 0)  # e.g. Friday, synced on Monday
            server.events = [make_event("EV-F", "Comite viernes", start, local_at(-3, 17, 0), teams_join=join,
                                        attendees=[attendee("Luis", "luis@example.invalid")])]
            server.online_meetings[join] = "MTG-1"
            server.transcripts["MTG-1"] = [{"id": "TR-1", "createdDateTime": fake_graph.iso_z(start)}]
            server.transcript_content["TR-1"] = VTT
            env = teams_env(tmp, server)
            result = env.sync("--profile", "acme")
            self.assertEqual(result.returncode, 0, result.stderr)
            md = env.vault / DEFAULT_RAW / "reuniones" / "acme" / f"{start:%Y-%m-%d}-comite-viernes.md"
            self.assertTrue(md.is_file())
            self.assertIn("reuniones 1", env.log_lines()[-1])
            show = run_fetch(env.env, "configure", "--show", "--profile", "acme")
            self.assertIn("meetings_lookback_days: 7 (source: default)", show.stdout)

    def test_p1_4_401_on_one_transcript_is_not_a_dead_session(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server() as server:
            inbox(server)
            server.messages = [make_message("m1", "uno", local_at(-1, 9, 0))]
            join = "https://teams.microsoft.com/l/meetup-join/abc"
            server.events = [make_event("EV-1", "Temprano", local_at(-1, 7, 0), local_at(-1, 7, 30), teams_join=join)]
            server.online_meetings[join] = "MTG-1"
            server.transcripts["MTG-1"] = [{"id": "TR-1"}]
            original = server.RequestHandlerClass._online_meetings

            def content_401(handler, rest, query):
                if len(rest) == 4 and rest[3] == "content":
                    handler._error(401, "InvalidAuthenticationToken")
                    return
                return original(handler, rest, query)

            server.RequestHandlerClass._online_meetings = content_401
            try:
                env = teams_env(tmp, server)
                result = env.sync("--profile", "acme")
            finally:
                server.RequestHandlerClass._online_meetings = original
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(mail_ids(env), ["m1"])
            self.assertFalse((env.config / "acme" / "needs-login").exists())
            line = env.log_lines()[-1]
            self.assertIn("sin acceso: 401", line)
            self.assertIn("estado ok", line)

    def test_p1_4_unexpected_error_in_calendar_does_not_cost_the_mail(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server("basic") as server:
            server.raw_responses["/me/calendarView"] = (200, "{esto no es json")
            env = SyncEnv(tmp, server)
            result = env.sync("--profile", "acme")
            self.assertEqual(result.returncode, 1)
            self.assertEqual(len(mail_ids(env)), 3)
            line = env.log_lines()[-1]
            self.assertIn("correo 3 nuevos", line)
            self.assertIn("estado error: calendario JSONDecodeError", line)
            self.assertFalse((env.vault / DEFAULT_LOCK).exists())

    def test_p1_4_timeouts_are_retried_and_logged_as_network(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server("basic") as server:
            server.delays["/me/calendarView"] = 2.5
            env = SyncEnv(tmp, server)
            slow = dict(env.env, INGEST_OUTLOOK_HTTP_TIMEOUT="1")
            result = env.sync("--profile", "acme", env=slow)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(len(mail_ids(env)), 3)
            self.assertIn("estado error: calendario red (", env.log_lines()[-1])
            attempts = sum(1 for _, path in server.paths if path.startswith("/me/calendarView"))
            self.assertEqual(attempts, 3)

    def test_finding_12_lock_path_mismatch_is_written_to_the_ingest_log(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server("basic") as server:
            env = SyncEnv(tmp, server, profiles=("acme", "globex"))
            run_fetch(env.env, "configure", "--profile", "globex", "--lock-path", "otra/ruta.lock")
            refused = env.sync("--all")
            self.assertEqual(refused.returncode, 2)
            lines = env.log_lines()
            self.assertTrue(any("| acme |" in l and "lock_path no coincide" in l for l in lines), lines)


class LockRegressions(unittest.TestCase):
    def test_finding_8_acquire_does_not_busy_spin_past_its_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = cerebro_lock.CerebroLock(tmp, "ingesta", wait_seconds=0.3, poll_seconds=0.05)
            outcome: list[bool] = []
            with mock.patch.object(cerebro_lock.CerebroLock, "_try_create", return_value=False), \
                    mock.patch.object(cerebro_lock, "read_lock", return_value=None):
                worker = threading.Thread(target=lambda: outcome.append(lock.acquire()), daemon=True)
                worker.start()
                worker.join(timeout=3)
                alive = worker.is_alive()
            self.assertFalse(alive, "acquire kept spinning after its 0.3 s deadline")
            self.assertEqual(outcome, [False])

    def test_finding_9_steal_does_not_destroy_a_lock_renewed_meanwhile(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = cerebro_lock.CerebroLock(tmp, "ingesta", wait_seconds=0.3, poll_seconds=0.05)
            lock.path.parent.mkdir(parents=True, exist_ok=True)
            old = (datetime.now().astimezone() - timedelta(minutes=25)).isoformat(timespec="seconds")
            lock.path.write_text(json.dumps({"paso": "extraccion", "pid": 1, "inicio": old, "equipo": "pc"}),
                                 encoding="utf-8")
            fresh = {"paso": "extraccion", "pid": 2, "inicio": datetime.now().astimezone().isoformat(timespec="seconds"),
                     "equipo": "pc"}
            state = {"swapped": False}
            real_replace, real_unlink = os.replace, Path.unlink

            def other_process_steals_first():
                # candado.py renamed the stale lock aside and took a fresh one
                if not state["swapped"]:
                    state["swapped"] = True
                    lock.path.write_text(json.dumps(fresh), encoding="utf-8")

            def replace(src, dst, *args, **kwargs):
                if Path(src) == lock.path:
                    other_process_steals_first()
                return real_replace(src, dst, *args, **kwargs)

            def unlink(path, *args, **kwargs):
                if Path(path) == lock.path:
                    other_process_steals_first()
                return real_unlink(path, *args, **kwargs)

            with mock.patch.object(cerebro_lock.os, "replace", side_effect=replace), \
                    mock.patch.object(Path, "unlink", autospec=True, side_effect=unlink):
                acquired = lock.acquire()
            self.assertFalse(acquired)
            self.assertEqual(json.loads(lock.path.read_text(encoding="utf-8")), fresh)
            self.assertEqual([p.name for p in lock.path.parent.iterdir()], [lock.path.name])  # no side files left

    def test_finding_10_release_reports_failure_and_cli_checks_pid_and_inicio(self):
        with tempfile.TemporaryDirectory() as tmp:
            notes: list[str] = []
            lock = cerebro_lock.CerebroLock(tmp, "ingesta", note=notes.append)
            self.assertTrue(lock.acquire())
            with mock.patch.object(Path, "unlink", side_effect=PermissionError("en uso")), \
                    mock.patch.object(cerebro_lock.time, "sleep"):
                self.assertFalse(lock.release())
            self.assertTrue(any("no se pudo borrar" in n for n in notes))
            lock.path.unlink()

            vault = Path(tmp) / "v"
            env = os.environ.copy()

            def cli(*args):
                return subprocess.run([sys.executable, str(LOCK_CLI), *args], capture_output=True, text=True,
                                      encoding="utf-8", timeout=30, env=env)

            info = json.loads(cli("acquire", "--vault", str(vault), "--paso", "extraccion", "--pid", "77").stdout)
            wrong = cli("release", "--vault", str(vault), "--paso", "extraccion", "--pid", "78",
                        "--inicio", info["inicio"])
            self.assertEqual(wrong.returncode, 1)
            self.assertTrue((vault / DEFAULT_LOCK).exists())
            right = cli("release", "--vault", str(vault), "--paso", "extraccion", "--pid", "77",
                        "--inicio", info["inicio"])
            self.assertEqual(right.returncode, 0, right.stderr)
            self.assertFalse((vault / DEFAULT_LOCK).exists())


class WindowsSchedulingRegressions(unittest.TestCase):
    def script(self) -> str:
        return schedule_win.build_register_script(schedule_win.ScheduleSpec(
            execute="C:\\py\\pythonw.exe", arguments='"C:\\f.py" sync --all', working_dir="C:\\",
        ))

    def test_p1_6_principal_uses_the_current_user_sid(self):
        script = self.script()
        self.assertIn("[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value", script)
        self.assertNotIn("GetCurrent().Name", script)

    def test_p1_6_any_failure_in_rewired_folder_falls_back_to_root(self):
        script = self.script()
        loop = script[script.index("foreach ($folder in $folders) {"):script.index("if ($denied)")]
        self.assertNotIn("exit 1", loop)  # no early exit: the root folder is always tried
        self.assertIn("continue", loop)

    def test_p1_6_and_finding_15_install_ps1_schedule_failure_warns_and_trims_vault(self):
        text = (ROOT / "install.ps1").read_text(encoding="utf-8-sig")
        self.assertNotIn('throw "fetch.py schedule install failed', text)
        block = text[text.index("if ($Schedule) {"):]
        self.assertIn("Write-Warning", block)
        self.assertIn("respaldo-hook-inicio.md", block)
        self.assertIn("No se pudo programar la ingesta automática", block)
        self.assertIn("$VaultArg.TrimEnd([char]'\\')", text)
        self.assertIn('"--vault-root", $VaultArg', text)
        self.assertNotIn('"--vault-root", $ResolvedVault', text)


if __name__ == "__main__":
    unittest.main()
