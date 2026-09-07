"""Avvio e attesa dell'helper C# WinRT (loopback :8766).

Il Python host è l'unico client previsto: WSL non deve vedere 8766.
Side-effect: processo figlio ``stt_helper.exe`` oppure ``dotnet run``.
Se l'helper è già in ascolto (debug a parte), non ne lanciamo un secondo.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import IO

_log = logging.getLogger("audio_host.helper")

# Cartella del csproj, accanto a questo file (windows_audio/stt_helper/).
DEFAULT_PROJECT_DIR = Path(__file__).resolve().parent / "stt_helper"
# Prima ``dotnet run`` può compilare: 15s non bastano, 90s sì su macchina fredda.
DEFAULT_READY_TIMEOUT_SEC = 90.0
DEFAULT_HELPER_PORT = 8766


def helper_health_url(port: int) -> str:
    """URL di probe. Solo 127.0.0.1: l'helper non bindà 0.0.0.0."""
    return f"http://127.0.0.1:{port}/health"


def helper_is_ready(port: int, *, timeout: float = 1.0) -> bool:
    """True se GET /health dell'helper risponde 2xx. Non alza: è un probe."""
    try:
        with urllib.request.urlopen(helper_health_url(port), timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (OSError, urllib.error.URLError, urllib.error.HTTPError):
        # Connection refused / timeout / 5xx: per il probe è "non pronto".
        return False


def find_helper_exe(project_dir: Path) -> Path | None:
    """Cerca ``stt_helper.exe`` già compilato sotto ``bin/``.

    Preferisce l'eseguibile più recente (Release vs Debug, RID publish).
    None se non c'è: il chiamante cadrà su ``dotnet run``.
    """
    bin_dir = project_dir / "bin"
    if not bin_dir.is_dir():
        return None
    # rglob: publish win-x64 mette l'exe un livello più in basso del TFM.
    candidates = [p for p in bin_dir.rglob("stt_helper.exe") if p.is_file()]
    if not candidates:
        return None
    # mtime: un rebuild recente vince su un Release stantio.
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _sources_newer_than(exe: Path, project_dir: Path) -> bool:
    """True se un ``.cs`` o il csproj è più nuovo dell'exe già in ``bin/``.

    Senza questo, dopo una patch C# ``server.py`` rilancerebbe l'exe stantio
    (form senza Load → /health morto) invece di ``dotnet run``.
    """
    try:
        exe_mtime = exe.stat().st_mtime
    except OSError:
        return True
    for pattern in ("*.cs", "*.csproj", "app.manifest"):
        for src in project_dir.glob(pattern):
            try:
                if src.stat().st_mtime > exe_mtime:
                    return True
            except OSError:
                continue
    return False


def find_dotnet() -> str | None:
    """``dotnet`` nel PATH, poi i path di installazione default Windows."""
    which = shutil.which("dotnet")
    if which:
        return which
    if sys.platform != "win32":
        return None
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    for base in (program_files, program_files_x86):
        candidate = Path(base) / "dotnet" / "dotnet.exe"
        if candidate.is_file():
            return str(candidate)
    return None


class HelperSupervisor:
    """Possiede (opzionalmente) il processo C# e aspetta /health prima di servire WSL.

    ``owned_proc`` è None se abbiamo riusato un helper già in ascolto, o se
    ``spawn=False``: in quel caso ``stop`` non uccide nulla.
    """

    def __init__(
        self,
        *,
        port: int = DEFAULT_HELPER_PORT,
        project_dir: Path | None = None,
        exe: Path | None = None,
        spawn: bool = True,
        ready_timeout_sec: float = DEFAULT_READY_TIMEOUT_SEC,
    ) -> None:
        self.port = port
        # Default accanto al server: windows_audio/stt_helper, non il cwd.
        self.project_dir = project_dir if project_dir is not None else DEFAULT_PROJECT_DIR
        # Override esplicito (env STT_HELPER_EXE / --helper-exe) batte la ricerca in bin/.
        self.exe = exe
        self.spawn = spawn
        self.ready_timeout_sec = ready_timeout_sec
        self.owned_proc: subprocess.Popen[bytes] | None = None
        # Log stdout/stderr di ``dotnet run`` (WinExe da solo non scrive qui).
        # Aperto in _spawn, chiuso in stop: non un ``with`` perché vive col figlio.
        self._log_file: IO[bytes] | None = None

    def ensure_running(self) -> None:
        """Fail-fast parlante se 8766 non diventa pronta. Idempotente sul riuso."""
        if helper_is_ready(self.port):
            _log.info(
                "Helper già in ascolto su 127.0.0.1:%s, non lo riavvio.",
                self.port,
            )
            return
        if not self.spawn:
            raise RuntimeError(
                f"Helper STT non risponde su 127.0.0.1:{self.port} e "
                "--no-spawn-helper è attivo. Avvia stt_helper a mano, oppure togli il flag."
            )
        self._spawn()
        self._wait_until_ready()

    def stop(self) -> None:
        """Termina solo il figlio che abbiamo spawnato noi. Idempotente."""
        proc = self.owned_proc
        self.owned_proc = None
        if proc is None:
            return
        if proc.poll() is not None:
            self._close_log()
            return
        _log.info("Arresto helper pid=%s", proc.pid)
        # terminate su Windows è TerminateProcess: WinExe non ha un handler Ctrl+C.
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _log.warning("Helper non termina, kill pid=%s", proc.pid)
            proc.kill()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                # Zombie raro: non blocchiamo lo shutdown del server Python.
                _log.warning("Helper ancora vivo dopo kill; proseguo.")
        self._close_log()

    def _spawn(self) -> None:
        """Lancia exe compilato se c'è, altrimenti ``dotnet run`` sul csproj."""
        cmd, cwd = self._build_command()
        log_path = Path(os.environ.get("TEMP", "/tmp")) / "audio_host_helper_spawn.log"
        # append: un riavvio del host non cancella il giro precedente (studio).
        self._log_file = open(log_path, "ab")  # noqa: SIM115 — chiuso in stop/_close_log
        _log.info("Spawn helper: %s (log %s)", " ".join(cmd), log_path)
        creationflags = 0
        if sys.platform == "win32":
            # NEW_PROCESS_GROUP: Ctrl+C nella console Python non abbatte il figlio
            # prima del nostro finally. WinExe non ha console, ma dotnet run sì.
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            self.owned_proc = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                stdout=self._log_file,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
        except OSError as exc:
            self._close_log()
            raise RuntimeError(f"Impossibile avviare l'helper: {exc}") from exc

    def _build_command(self) -> tuple[list[str], Path]:
        """Comando + cwd. Gli env WAKE_* li eredita il figlio dal processo Python."""
        exe = self.exe if self.exe is not None else find_helper_exe(self.project_dir)
        # --helper-exe / STT_HELPER_EXE: l'utente ha scelto quell'eseguibile, non ricompiliamo.
        forced_exe = self.exe is not None
        if exe is not None and exe.is_file() and (forced_exe or not _sources_newer_than(exe, self.project_dir)):
            return [str(exe), f"--port={self.port}"], exe.parent
        if exe is not None and not exe.is_file() and forced_exe:
            raise RuntimeError(f"STT_HELPER_EXE non trovato: {exe}")
        if exe is not None and not forced_exe:
            _log.info(
                "Sorgenti C# più recenti di %s: ricompilo con dotnet run.",
                exe,
            )

        csproj = self.project_dir / "stt_helper.csproj"
        if not csproj.is_file():
            raise RuntimeError(
                f"Manca {csproj} e non c'è stt_helper.exe. "
                "Compila l'helper su Windows (SDK .NET 8) oppure passa --helper-exe."
            )
        dotnet = find_dotnet()
        if dotnet is None:
            raise RuntimeError(
                "Né stt_helper.exe né dotnet nel PATH. Installa SDK .NET 8 "
                "oppure copia l'exe in stt_helper/bin/."
            )
        # --no-launch-profile: niente launchSettings.json a inquinare la porta.
        # -- dopo i flag MSBuild: gli arg --port arrivano a Program.Main.
        cmd = [
            dotnet,
            "run",
            "--project",
            str(csproj),
            "--no-launch-profile",
            "--",
            f"--port={self.port}",
        ]
        return cmd, self.project_dir

    def _wait_until_ready(self) -> None:
        """Poll /health finché risponde, il processo muore, o scade il tetto."""
        deadline = time.monotonic() + self.ready_timeout_sec
        _log.info(
            "Attendo helper su 127.0.0.1:%s (timeout %.0fs, prima compilazione può essere lenta).",
            self.port,
            self.ready_timeout_sec,
        )
        while time.monotonic() < deadline:
            proc = self.owned_proc
            if proc is not None and proc.poll() is not None:
                raise RuntimeError(
                    f"Helper uscito subito (codice {proc.returncode}). "
                    "Vedi %TEMP%\\audio_host_helper_spawn.log e stt_helper.log."
                )
            if helper_is_ready(self.port):
                _log.info("Helper pronto su 127.0.0.1:%s", self.port)
                return
            time.sleep(0.4)
        # Timeout: uccidiamo il figlio così un retry non trova la porta a metà.
        self.stop()
        raise RuntimeError(
            f"Helper non ha risposto su /health entro {self.ready_timeout_sec:.0f}s. "
            "Serve SDK .NET 8, microfono e riconoscimento vocale online (vedi docs)."
        )

    def _close_log(self) -> None:
        """Chiude il file di spawn senza mascherare errori di stop."""
        handle = self._log_file
        self._log_file = None
        if handle is None:
            return
        try:
            handle.close()
        except OSError:
            pass
