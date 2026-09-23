#!/usr/bin/env python3
"""
cerebro_lock.py, one lock per vault for the three "cerebro" processes.

Author:   Danny Bravo
License:  MIT
Project:  https://github.com/danilobrando/ingest-outlook

Implements the lock agreed in the System 3 v0.2 contract (sections 4 and 7):

- File `<vault>/<lock_path>` (default `.claude/system3/cerebro.lock`, the
  System 3 canonical layout; the vault's `.claude/system3/config.json` may
  declare another `lock_path`), created with an exclusive open (O_CREAT | O_EXCL).
- Content: JSON {"paso", "perfil", "pid", "inicio", "equipo"}.
- A lock older than 20 minutes is abandoned: the next process steals it by
  renaming it aside (atomic; the same way the extraction's candado.py does),
  checks that what it moved is the stale lock it judged, and leaves a note in
  its own log.
- A lock that is held and still valid is retried for up to 5 minutes; after
  that the caller skips its run (it does not fail).
- Every process releases it when it finishes, also on error (use the context
  manager or try/finally).

The same module serves ingest-outlook (`fetch.py sync`), the automatic
extraction and the s3 index, so all three follow identical rules:

    python cerebro_lock.py acquire --vault V --paso extraccion [--lock-path P]
    python cerebro_lock.py release --vault V --paso extraccion --pid N --inicio ISO [--lock-path P]
    python cerebro_lock.py status  --vault V [--lock-path P]

`acquire` prints the lock JSON; pass its `pid` and `inicio` back to `release`,
which only removes the lock when paso, pid and inicio all match (or --force).

Exit codes: 0 ok, 2 usage error, 3 lock busy (acquire skipped after waiting),
1 release refused (another process holds the lock) or the lock file could not
be removed.

Environment overrides (tests and advanced use): CEREBRO_LOCK_WAIT_SECONDS,
CEREBRO_LOCK_STALE_SECONDS, CEREBRO_LOCK_POLL_SECONDS.

Stdlib only.
"""
from __future__ import annotations

__author__ = "Danny Bravo"
__version__ = "0.6.0"
__license__ = "MIT"

import argparse
import json
import os
import re
import secrets
import socket
import sys
import time
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable

DEFAULT_LOCK_PATH = ".claude/system3/cerebro.lock"
STALE_SECONDS = 20 * 60
WAIT_SECONDS = 5 * 60
POLL_SECONDS = 1.0
RELEASE_ATTEMPTS = 10

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_BUSY = 3

_PASO_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def validate_relative_path(value: str, label: str = "path") -> str:
    """Accept a vault-relative path with forward or back slashes.

    Rejects empty, absolute, drive-qualified, '.'/'..' components, Windows
    reserved names and characters Windows cannot store. Dot-prefixed
    directories such as `.claude` are allowed.
    """
    text = str(value or "").strip().replace("\\", "/")
    posix_path = PurePosixPath(text)
    windows_path = PureWindowsPath(text)
    if not text or posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise ValueError(f"{label} must be a non-empty path relative to the vault")
    if any(part in {"", ".", ".."} for part in posix_path.parts):
        raise ValueError(f"{label} must not contain '.' or '..' components")
    for part in posix_path.parts:
        if re.search(r'[:*?"<>|]', part) or part.endswith((".", " ")):
            raise ValueError(f"{label} contains a Windows-unsafe component: {part!r}")
        if part.split(".")[0].lower() in _WINDOWS_RESERVED:
            raise ValueError(f"{label} contains a Windows reserved name: {part!r}")
    return text.rstrip("/")


def resolve_lock_path(vault: str | os.PathLike[str], lock_path: str = DEFAULT_LOCK_PATH) -> Path:
    return Path(vault) / Path(*PurePosixPath(validate_relative_path(lock_path, "lock_path")).parts)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _hostname() -> str:
    try:
        return socket.gethostname() or "desconocido"
    except OSError:
        return "desconocido"


def read_lock(path: Path) -> dict[str, Any] | None:
    """Return the lock content, None when the file does not exist.

    A file that exists but cannot be parsed (another process is between the
    exclusive create and the write) comes back as {"_ilegible": True}.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        # Windows: sharing violation or delete pending. Treat as held.
        return {"_ilegible": True}
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"_ilegible": True}
    if not isinstance(data, dict):
        return {"_ilegible": True}
    return data


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def lock_age_seconds(info: dict[str, Any] | None, path: Path) -> float | None:
    """Age from `inicio`; falls back to the file mtime when `inicio` is missing,
    unparseable, or in the future (clock skew between machines)."""
    now = time.time()
    started = _parse_iso((info or {}).get("inicio"))
    if started is not None:
        age = now - started.timestamp()
        if age >= -60:
            return max(0.0, age)
    try:
        return max(0.0, now - path.stat().st_mtime)
    except OSError:
        return None


def describe_holder(info: dict[str, Any] | None) -> str:
    if not info:
        return "desconocido"
    if info.get("_ilegible"):
        return "desconocido (lock en escritura)"
    return str(info.get("paso") or "desconocido")


class CerebroLock:
    """Exclusive vault lock. Use `acquire()`/`release()` or `with`.

    `acquire()` returns False when the lock stayed busy for the whole wait:
    the caller must skip its run. `stolen` holds the abandoned lock content
    when this process stole it; `note` receives one human line per event.
    """

    def __init__(
        self,
        vault: str | os.PathLike[str],
        paso: str,
        perfil: str | None = None,
        lock_path: str = DEFAULT_LOCK_PATH,
        pid: int | None = None,
        wait_seconds: float | None = None,
        stale_seconds: float | None = None,
        poll_seconds: float | None = None,
        note: Callable[[str], None] | None = None,
    ):
        if not _PASO_RE.fullmatch(str(paso or "")):
            raise ValueError("paso must match [a-z0-9][a-z0-9_-]{0,31}")
        self.vault = Path(vault)
        self.paso = paso
        self.perfil = perfil
        self.path = resolve_lock_path(self.vault, lock_path)
        self.pid = int(pid) if pid is not None else os.getpid()
        self.wait_seconds = (
            wait_seconds if wait_seconds is not None
            else _env_float("CEREBRO_LOCK_WAIT_SECONDS", WAIT_SECONDS)
        )
        self.stale_seconds = (
            stale_seconds if stale_seconds is not None
            else _env_float("CEREBRO_LOCK_STALE_SECONDS", STALE_SECONDS)
        )
        self.poll_seconds = (
            poll_seconds if poll_seconds is not None
            else _env_float("CEREBRO_LOCK_POLL_SECONDS", POLL_SECONDS)
        ) or 0.05
        self.note = note or (lambda _msg: None)
        self.info: dict[str, Any] | None = None
        self.stolen: dict[str, Any] | None = None
        self.holder: dict[str, Any] | None = None
        self.waited_seconds = 0.0

    # -- internals ---------------------------------------------------------

    def _content(self) -> dict[str, Any]:
        return {
            "paso": self.paso,
            "perfil": self.perfil,
            "pid": self.pid,
            "inicio": _now_iso(),
            "equipo": _hostname(),
        }

    def _try_create(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = self._content()
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(self.path, flags, 0o644)
        except FileExistsError:
            return False
        except PermissionError:
            # Windows reports a file in "delete pending" state this way.
            return False
        try:
            os.write(fd, (json.dumps(content, ensure_ascii=False) + "\n").encode("utf-8"))
            try:
                os.fsync(fd)
            except OSError:
                pass
        finally:
            os.close(fd)
        self.info = content
        return True

    def _is_stale(self, info: dict[str, Any]) -> bool:
        age = lock_age_seconds(info, self.path)
        return age is not None and age > self.stale_seconds

    def _side_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.robado.{os.getpid()}.{secrets.token_hex(4)}")

    def _restore(self, side: Path) -> bool:
        """Put back a lock we moved by mistake, without clobbering a newer one."""
        try:
            os.link(side, self.path)  # fails if a lock already exists again
        except FileExistsError:
            return False
        except (OSError, AttributeError, NotImplementedError):
            try:
                if self.path.exists():
                    return False
                os.rename(side, self.path)
                return True
            except OSError:
                return False
        try:
            side.unlink()
        except OSError:
            pass
        return True

    def _steal(self, expected: dict[str, Any]) -> bool:
        """Steal an abandoned lock atomically: rename it aside, then check that
        the file moved is the stale lock read before. If another process
        renewed the lock in between, put theirs back and keep waiting."""
        side = self._side_path()
        try:
            os.replace(self.path, side)
        except FileNotFoundError:
            return False  # released or stolen by someone else meanwhile
        except OSError:
            return False  # Windows: open by a reader; retry on the next poll
        moved = read_lock(side)
        if moved != expected:
            if not self._restore(side):
                self.note("al robar el lock se movió uno recién tomado por otro paso y no se pudo devolver")
            try:
                side.unlink()
            except OSError:
                pass
            return False
        try:
            side.unlink()
        except OSError:
            pass
        if not self._try_create():
            return False
        self.stolen = expected
        age = lock_age_seconds(expected, side)
        self.note(
            "lock abandonado robado (paso {paso}, inicio {inicio})".format(
                paso=describe_holder(expected),
                inicio=expected.get("inicio") or "?",
            )
            + (f", tenía {int(age // 60)} min" if age is not None else "")
        )
        return True

    # -- public API --------------------------------------------------------

    def acquire(self) -> bool:
        start = time.monotonic()
        deadline = start + max(0.0, self.wait_seconds)
        while True:
            if self._try_create():
                self.waited_seconds = time.monotonic() - start
                return True
            current = read_lock(self.path)
            if current is not None and self._is_stale(current) and self._steal(current):
                self.waited_seconds = time.monotonic() - start
                return True
            now = time.monotonic()
            if now >= deadline:
                self.holder = read_lock(self.path) or current
                self.waited_seconds = now - start
                return False
            # A lock that vanished between create and read is retried soon,
            # but never in a busy loop; the deadline is always honored.
            pause = 0.05 if current is None else self.poll_seconds
            time.sleep(min(pause, max(0.01, deadline - now)))

    def owns(self, current: dict[str, Any] | None) -> bool:
        if not current or not self.info:
            return False
        keys = ("paso", "pid", "inicio", "equipo")
        return all(current.get(k) == self.info.get(k) for k in keys)

    def release(self) -> bool:
        """Remove the lock if it is still ours. Returns False when someone
        else holds it now (ours was stolen after 20 min) and leaves it alone."""
        if self.info is None:
            return True
        current = read_lock(self.path)
        if current is None:
            self.info = None
            return True
        if not self.owns(current):
            self.note("el lock ya no era de este proceso (lo tomó otro paso); no se borra")
            self.info = None
            return False
        for attempt in range(RELEASE_ATTEMPTS):
            try:
                self.path.unlink()
                self.info = None
                return True
            except FileNotFoundError:
                self.info = None
                return True
            except OSError:
                time.sleep(0.1 * (attempt + 1))
        self.note(
            f"no se pudo borrar el lock {self.path} al terminar; quedará tomado hasta que "
            "otro paso lo dé por abandonado (20 min)"
        )
        return False

    def __enter__(self) -> "CerebroLock":
        if not self.acquire():
            raise LockBusy(self.holder)
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.release()


class LockBusy(Exception):
    def __init__(self, holder: dict[str, Any] | None):
        super().__init__(f"lock ocupado por {describe_holder(holder)}")
        self.holder = holder


def lock_status(
    vault: str | os.PathLike[str],
    lock_path: str = DEFAULT_LOCK_PATH,
    stale_seconds: float | None = None,
) -> dict[str, Any]:
    path = resolve_lock_path(vault, lock_path)
    stale = stale_seconds if stale_seconds is not None else _env_float(
        "CEREBRO_LOCK_STALE_SECONDS", STALE_SECONDS
    )
    info = read_lock(path)
    if info is None:
        return {"estado": "libre", "ruta": str(path)}
    age = lock_age_seconds(info, path)
    estado = "abandonado" if age is not None and age > stale else "tomado"
    return {
        "estado": estado,
        "ruta": str(path),
        "lock": info,
        "edad_segundos": int(age) if age is not None else None,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _configure_console_encoding() -> None:
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if stream is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def _append_log(vault: Path, log_rel: str | None, message: str) -> None:
    if not log_rel:
        return
    try:
        log_path = Path(vault) / Path(*PurePosixPath(validate_relative_path(log_rel, "log")).parts)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(message + "\n")
    except (OSError, ValueError) as exc:
        print(f"no se pudo escribir en el log {log_rel}: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    _configure_console_encoding()
    parser = argparse.ArgumentParser(
        description="Lock único por vault (contrato System 3 v0.2).",
    )
    parser.add_argument("--version", action="version", version=f"cerebro_lock.py v{__version__}")
    sub = parser.add_subparsers(dest="action", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--vault", required=True, help="Raíz del vault.")
        p.add_argument(
            "--lock-path", default=DEFAULT_LOCK_PATH,
            help=f"Ruta del lock relativa al vault (default {DEFAULT_LOCK_PATH}).",
        )

    p_acq = sub.add_parser("acquire", help="Toma el lock (espera hasta 5 min; si sigue ocupado sale con 3).")
    common(p_acq)
    p_acq.add_argument("--paso", required=True, help="ingesta | extraccion | s3")
    p_acq.add_argument("--perfil", default=None)
    p_acq.add_argument(
        "--pid", type=int, default=None,
        help="PID a registrar (default: el proceso que invocó este comando).",
    )
    p_acq.add_argument("--wait", type=float, default=None, help="Segundos de espera (default 300).")
    p_acq.add_argument("--stale", type=float, default=None, help="Segundos para darlo por abandonado (default 1200).")
    p_acq.add_argument("--log", default=None, help="Log relativo al vault donde anotar robos y saltos.")

    p_rel = sub.add_parser(
        "release",
        help="Libera el lock si paso, pid e inicio coinciden con los que imprimió acquire.",
    )
    common(p_rel)
    p_rel.add_argument("--paso", required=True)
    p_rel.add_argument("--pid", type=int, default=None, help="pid que imprimió acquire.")
    p_rel.add_argument("--inicio", default=None, help="inicio que imprimió acquire.")
    p_rel.add_argument("--force", action="store_true", help="Borra el lock aunque sea de otro paso o equipo.")

    p_st = sub.add_parser("status", help="Muestra el estado del lock en JSON.")
    common(p_st)
    p_st.add_argument("--stale", type=float, default=None)

    args = parser.parse_args(argv)
    try:
        vault = Path(args.vault).expanduser()
        if args.action == "status":
            print(json.dumps(lock_status(vault, args.lock_path, args.stale), ensure_ascii=False, indent=2))
            return EXIT_OK

        if args.action == "acquire":
            notes: list[str] = []
            pid = args.pid if args.pid is not None else (os.getppid() if hasattr(os, "getppid") else os.getpid())
            lock = CerebroLock(
                vault, args.paso, perfil=args.perfil, lock_path=args.lock_path, pid=pid,
                wait_seconds=args.wait, stale_seconds=args.stale, note=notes.append,
            )
            acquired = lock.acquire()
            for line in notes:
                print(line, file=sys.stderr)
                _append_log(vault, args.log, f"{_now_iso()} | {args.paso} | {line}")
            if acquired:
                print(json.dumps(lock.info, ensure_ascii=False))
                return EXIT_OK
            message = f"saltada: lock ocupado por {describe_holder(lock.holder)}"
            print(message, file=sys.stderr)
            _append_log(vault, args.log, f"{_now_iso()} | {args.paso} | {message}")
            return EXIT_BUSY

        # release
        path = resolve_lock_path(vault, args.lock_path)
        current = read_lock(path)
        if current is None:
            print("libre: no había lock")
            return EXIT_OK
        if not args.force and (args.pid is None or not args.inicio):
            print("ERROR: release necesita --pid y --inicio (los que imprimió acquire) o --force",
                  file=sys.stderr)
            return EXIT_USAGE
        mine = (
            current.get("paso") == args.paso
            and current.get("equipo") == _hostname()
            and current.get("pid") == args.pid
            and current.get("inicio") == args.inicio
        )
        if not mine and not args.force:
            print(
                f"no se libera: el lock es de {describe_holder(current)} en "
                f"{current.get('equipo') or '?'} (pid {current.get('pid')}, inicio "
                f"{current.get('inicio')}); usa --force solo si sabes que está abandonado",
                file=sys.stderr,
            )
            return EXIT_REFUSED
        for attempt in range(RELEASE_ATTEMPTS):
            try:
                path.unlink()
                break
            except FileNotFoundError:
                break
            except OSError as exc:
                if attempt == RELEASE_ATTEMPTS - 1:
                    print(f"ERROR: no se pudo borrar el lock {path}: {exc}", file=sys.stderr)
                    return EXIT_REFUSED
                time.sleep(0.1 * (attempt + 1))
        print("liberado")
        return EXIT_OK
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
