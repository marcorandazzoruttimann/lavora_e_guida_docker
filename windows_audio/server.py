"""Host audio Windows: HTTP :8765 verso WSL, helper C# :8766, TTS locale.

Contratto identico a ``HttpBridgeSTT`` / ``HttpBridgeTTS`` (WSL non cambia):

- ``POST /listen`` → wake helper :8766 → Vosk it-IT → ``{"transcript": "..."}``
- ``POST /speak``  → body ``{"text": "..."}`` → edge-tts + playback → 200
- ``GET /health``  → Python + probe helper

Half-duplex: un ``threading.Lock`` copre listen e speak. Il microfono WinRT
non deve sentire Elsa. ``/health`` non prende il lock. All'avvio, prima del
bind, ``warmup_synthesis`` paga il freddo di edge-tts (minuti) senza parlato.

Avvio unico: questo processo spawn l'helper (exe in bin/ o ``dotnet run``).
Non aggiungere dipendenze a ``pyproject.toml`` WSL: venv host + requirements.txt.

Esecuzione: ``python.exe server.py`` su Windows (idealmente da disco ``C:\\``,
non solo ``\\\\wsl$\\``). Da WSL questo file si può importare nei test.
"""

from __future__ import annotations

import argparse
import http.client
import http.server
import json
import logging
import os
import socket
import socketserver
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from helper_spawn import (
    DEFAULT_HELPER_PORT,
    HelperSupervisor,
    helper_is_ready,
)

_log = logging.getLogger("audio_host")

# Bind all-interfaces: WSL in NAT raggiunge l'IP del nameserver, non 127.0.0.1.
DEFAULT_BIND = "0.0.0.0"
DEFAULT_PORT = 8765
# Voce allineata al piano e a tts_playback.DEFAULT_VOICE (import lazy).
DEFAULT_VOICE = "it-IT-ElsaNeural"
# Inoltro /listen: None = aspetta l'helper. Il tetto vero è AUDIO_LISTEN_TIMEOUT_SEC
# lato WSL (300s). Un timeout qui più corto farebbe 504 mentre l'utente tace.
DEFAULT_LISTEN_FORWARD_TIMEOUT: float | None = None


SpeakFn = Callable[[str, str], None]


@dataclass
class HostState:
    """Stato condiviso dai thread di ``ThreadingMixIn``. Un processo = un state."""

    # Un solo I/O audio alla volta: listen XOR speak.
    half_duplex: threading.Lock
    # True durante playback: /health lo espone, utile per capire un listen in coda.
    speaking: threading.Event
    helper_port: int
    # None = urlopen/HTTPConnection senza tetto (WSL è il watchdog).
    listen_forward_timeout: float | None
    tts_voice: str
    # Iniettabile nei test: evita edge-tts/pygame sul runner WSL.
    speak_fn: SpeakFn | None = None
    # Iniettabile: evita Vosk/mic nei test. None = stt_vosk.run_dictation.
    dictate_fn: Callable[..., object] | None = None
    wake_close: str = "assistente chiudi"
    wake_cancel: str = "assistente annulla"


def _default_speak(text: str, voice: str) -> None:
    """Import lazy: ``import server`` nei test WSL non richiede pygame."""
    from tts_playback import synthesize_and_play

    synthesize_and_play(text, voice=voice)


def peer_disconnected(sock: socket.socket) -> bool:
    """True se WSL ha chiuso (timeout httpx) o il socket è morto.

    Serve a rilasciare il mutex half-duplex: altrimenti un /listen orfano
    terrebbe il lock finché qualcuno dice «ehi assistente». MSG_PEEK non
    consuma byte; BaseHTTPRequestHandler può ancora leggere il body.
    """
    try:
        old_timeout = sock.gettimeout()
    except OSError:
        return True
    try:
        # 0 = non bloccante. Se non c'è nulla da leggere, BlockingIOError = ancora vivo.
        sock.settimeout(0)
        pending = sock.recv(1, socket.MSG_PEEK)
    except BlockingIOError:
        return False
    except TimeoutError:
        # Alcuni backend Windows traducono il non-block in timeout vuoto: ancora vivo.
        return False
    except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
        return True
    finally:
        try:
            sock.settimeout(old_timeout)
        except OSError:
            return True
    # recv di zero byte = FIN. Il client non leggerà più la risposta.
    return pending == b""


class ClientGone(Exception):
    """WSL ha staccato mentre inoltravamo /listen. Non scrivere sul socket morto."""


class AudioHostHandler(http.server.BaseHTTPRequestHandler):
    """Una request = un thread. Il lock sta su ``HostState``, non sul handler."""

    # HTTP/1.1: httpx di WSL lo parla. Chiudiamo comunque: /listen è long-poll.
    protocol_version = "HTTP/1.1"
    # Niente timeout socket sul /listen da 5 minuti. Il default di HTTPServer
    # (None) va bene; lo ribadiamo così un refactor non reintroduce 30s.
    timeout = None

    @property
    def state(self) -> HostState:
        # AudioHostServer.state è assegnato prima di serve_forever.
        return self.server.state  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        path = self._path_only()
        if path in ("/health", "/"):
            self._handle_health()
            return
        self._json(404, {"error": "non trovato"})

    def do_POST(self) -> None:
        path = self._path_only()
        if path == "/listen":
            self._handle_listen()
            return
        if path == "/speak":
            self._handle_speak()
            return
        self._json(404, {"error": "non trovato"})

    def log_message(self, fmt: str, *args: object) -> None:
        # Access log via logger di processo, non stderr nudo di BaseHTTPRequestHandler.
        _log.info("%s " + fmt, self.address_string(), *args)

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        # /health è un probe: non intasare la console a ogni GET da WSL o dallo spawn.
        path = self._path_only()
        if path in ("/health", "/") and str(code) in ("200", "503"):
            return
        super().log_request(code, size)

    def _path_only(self) -> str:
        # self.path include query string; rstrip così /listen/ e /listen coincidono.
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        return path if path else "/"

    def _handle_health(self) -> None:
        """Liveness Python + readiness helper. Senza lock: non deve aspettare il TTS."""
        helper_ok = helper_is_ready(self.state.helper_port, timeout=0.8)
        payload = {
            "ok": helper_ok,
            "helper": helper_ok,
            "speaking": self.state.speaking.is_set(),
        }
        # 503 se l'helper è caduto: curl -f e un eventuale watchdog WSL falliscono.
        self._json(200 if helper_ok else 503, payload)

    def _handle_listen(self) -> None:
        """Wake WinRT (helper) poi dettatura Vosk. Loop annulla/timeout vuoto."""
        state = self.state
        _log.info("POST /listen: attendo mutex half-duplex (niente mic mentre parla).")
        with state.half_duplex:
            try:
                self._listen_wake_then_dictate(state)
            except ClientGone:
                _log.info("Client WSL staccato durante /listen; mutex libero.")

    def _listen_wake_then_dictate(self, state: HostState) -> None:
        """Un turno WSL: ripete wake+dettatura finché c'è un transcript."""
        while True:
            if peer_disconnected(self.connection):
                raise ClientGone()
            _log.info("POST /listen: inoltro wake a 127.0.0.1:%s", state.helper_port)
            try:
                status, body = _forward_listen_abortable(
                    helper_port=state.helper_port,
                    timeout=state.listen_forward_timeout,
                    client_sock=self.connection,
                )
            except (OSError, http.client.HTTPException) as exc:
                _log.warning("Helper irraggiungibile su /listen: %s", exc)
                self._json(
                    502,
                    {"error": f"helper STT non raggiungibile su 127.0.0.1:{state.helper_port}"},
                )
                return

            if status == 503:
                _log.info("Helper 503 (listen preemptato).")
                self._bytes(503, body)
                return
            if status != 200:
                _log.warning("Helper /listen status=%s", status)
                self._bytes(502 if status >= 400 else status, body)
                return

            # Test / helper vecchio: JSON con transcript già pronto, niente Vosk.
            transcript = _transcript_from_helper_body(body)
            if transcript is not None:
                _log.info("POST /listen ok (transcript helper), %d caratteri.", len(transcript))
                self._bytes(200, body)
                return
            if not _helper_awake(body):
                _log.warning("Helper 200 senza awake né transcript.")
                self._json(502, {"error": "helper ha restituito un body ascolto non utilizzabile"})
                return

            _log.info("Wake ok, dettatura Vosk.")
            try:
                outcome = _run_host_dictation(state, lambda: peer_disconnected(self.connection))
            except RuntimeError as exc:
                if "staccato" in str(exc):
                    raise ClientGone() from exc
                _log.warning("Dettatura fallita: %s", exc)
                self._json(502, {"error": str(exc)})
                return

            if outcome.kind in ("close", "timeout_text") and outcome.text.strip():
                _log.info("POST /listen ok, %d caratteri.", len(outcome.text))
                self._json(200, {"transcript": outcome.text})
                return
            if outcome.kind == "cancel":
                try:
                    import winsound

                    winsound.PlaySound("SystemHand", winsound.SND_ALIAS | winsound.SND_ASYNC)
                except Exception:
                    pass
                _log.info("Annulla: buffer scartato, torno alla wake.")
                continue
            _log.info("Dettatura senza testo, torno alla wake.")
            continue

    def _handle_speak(self) -> None:
        """Sintesi + play sotto mutex. 200 solo a playback finito (contratto WSL)."""
        raw = self._read_body()
        try:
            payload: Any = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"error": "body JSON non valido"})
            return
        if not isinstance(payload, dict):
            self._json(400, {"error": "body JSON deve essere un oggetto con chiave text"})
            return
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            # WSL manda sempre una frase; vuoto/whitespace = errore, non silenzio.
            self._json(400, {"error": "campo text obbligatorio e non vuoto"})
            return

        state = self.state
        speak = state.speak_fn or _default_speak
        _log.info("POST /speak: attendo mutex half-duplex, %d caratteri.", len(text.strip()))
        with state.half_duplex:
            state.speaking.set()
            try:
                speak(text, state.tts_voice)
            except Exception as exc:  # noqa: BLE001 — bordo HTTP: edge-tts/pygame alzano di tutto
                # Rete Microsoft, device audio, MP3 vuoto: il processo deve restare su.
                _log.exception("Sintesi o playback falliti")
                self._json(502, {"error": f"tts fallito: {exc}"})
                return
            finally:
                state.speaking.clear()
        _log.info("POST /speak ok.")
        self._json(200, {"ok": True})

    def _read_body(self) -> bytes:
        """Legge Content-Length byte. Senza header: body vuoto (POST /listen)."""
        length_raw = self.headers.get("Content-Length", "0")
        try:
            length = int(length_raw)
        except ValueError:
            return b""
        if length <= 0:
            return b""
        # Tetto largo: una reply parlata non è un allegato. 1 MiB basta e evita
        # un client che dichiara Content-Length enorme e tiene il thread.
        if length > 1_000_000:
            return b""
        return self.rfile.read(length)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        self._bytes(status, _json_dumps(payload))

    def _bytes(self, status: int, body: bytes) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # close: /listen long-poll non ha senso in keep-alive con http.server.
            self.send_header("Connection", "close")
            self.end_headers()
            if body and self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            # Client già andato (timeout WSL): non è un crash del host.
            _log.info("Socket client chiuso durante la risposta %s.", status)


def _json_dumps(payload: dict[str, Any]) -> bytes:
    # ensure_ascii=False: transcript italiano leggibile; WSL json() accetta UTF-8.
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _helper_awake(body: bytes) -> bool:
    """True se l'helper ha chiuso la wake (JSON ``awake``) e il mic è libero."""
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("awake") is True


def _run_host_dictation(state: HostState, should_stop: Callable[[], bool]) -> Any:
    """Vosk sul device di default, o ``dictate_fn`` nei test (niente mic)."""
    if state.dictate_fn is not None:
        return state.dictate_fn(should_stop)
    from stt_vosk import run_dictation

    return run_dictation(
        wake_close=state.wake_close,
        wake_cancel=state.wake_cancel,
        should_stop=should_stop,
    )


def _transcript_from_helper_body(body: bytes) -> str | None:
    """Estrae un transcript non vuoto. None → il handler deve fare 502, non 200."""
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    transcript = payload.get("transcript")
    if not isinstance(transcript, str):
        return None
    if not transcript.strip():
        return None
    return transcript


def _forward_listen_abortable(
    *,
    helper_port: int,
    timeout: float | None,
    client_sock: socket.socket,
) -> tuple[int, bytes]:
    """POST /listen all'helper in un worker; abort se WSL stacca.

    Chiudere la socket verso C# non ferma WinRT (serve un nuovo /listen per
    preempt). Serve a sbloccare *questo* thread e il mutex.
    """
    box: dict[str, Any] = {}
    done = threading.Event()
    conn_holder: dict[str, http.client.HTTPConnection | None] = {"conn": None}

    def worker() -> None:
        conn = http.client.HTTPConnection("127.0.0.1", helper_port, timeout=timeout)
        conn_holder["conn"] = conn
        try:
            # Body vuoto come HttpBridgeSTT: l'helper non legge il POST.
            conn.request(
                "POST",
                "/listen",
                body=b"",
                headers={"Content-Length": "0", "Connection": "close"},
            )
            resp = conn.getresponse()
            box["status"] = resp.status
            box["body"] = resp.read()
        except Exception as exc:  # noqa: BLE001 — marshal verso il thread HTTP
            # Il worker non può alzare nel handler: mettiamo l'eccezione nel box.
            box["exc"] = exc
        finally:
            try:
                conn.close()
            except OSError:
                pass
            done.set()

    threading.Thread(target=worker, daemon=True, name="listen-forward").start()
    # 0.4s: abbastanza frequente da rilasciare il mutex poco dopo il timeout WSL,
    # abbastanza lento da non bruciare CPU per 5 minuti di attesa wake.
    while not done.wait(timeout=0.4):
        if peer_disconnected(client_sock):
            held = conn_holder["conn"]
            if held is not None:
                try:
                    held.close()
                except OSError:
                    pass
            raise ClientGone()
    if "exc" in box:
        raise box["exc"]
    return int(box.get("status", 502)), bytes(box.get("body", b""))


class AudioHostServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Un thread per request: /health non aspetta /listen. daemon: Ctrl+C esce."""

    daemon_threads = True
    # Python 3.7+: shutdown non si blocca sui /listen ancora in volo.
    block_on_close = False
    allow_reuse_address = True
    state: HostState


def build_server(
    *,
    bind: str,
    port: int,
    state: HostState,
) -> AudioHostServer:
    """Costruisce il server senza servirlo. I test usano ``port=0`` (ephemeral)."""
    server = AudioHostServer((bind, port), AudioHostHandler)
    server.state = state
    return server


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """CLI + env. Gli env WAKE_* non stanno qui: li eredita l'helper spawnato."""
    parser = argparse.ArgumentParser(
        description=(
            "Host audio Windows per lavora_e_guida: HTTP :8765 (WSL), "
            "helper STT :8766, TTS edge-tts."
        )
    )
    parser.add_argument(
        "--bind",
        default=os.environ.get("AUDIO_HOST_BIND", DEFAULT_BIND),
        help="Indirizzo di ascolto (default 0.0.0.0, raggiungibile da WSL NAT).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("AUDIO_HOST_PORT", str(DEFAULT_PORT))),
        help="Porta verso WSL (default 8765).",
    )
    parser.add_argument(
        "--helper-port",
        type=int,
        default=int(os.environ.get("STT_HELPER_PORT", str(DEFAULT_HELPER_PORT))),
        help="Porta loopback dell'helper C# (default 8766).",
    )
    parser.add_argument(
        "--helper-exe",
        default=os.environ.get("STT_HELPER_EXE") or None,
        help="Path di stt_helper.exe già compilato. Se omesso: bin/ oppure dotnet run.",
    )
    parser.add_argument(
        "--no-spawn-helper",
        action="store_true",
        help="Non lanciare l'helper: deve essere già in ascolto su --helper-port.",
    )
    parser.add_argument(
        "--voice",
        default=os.environ.get("TTS_VOICE", DEFAULT_VOICE),
        help="Voce edge-tts (default it-IT-ElsaNeural).",
    )
    parser.add_argument(
        "--allow-non-windows",
        action="store_true",
        help="Salta il fail-fast win32 (solo test / debug). WinRT e pygame restano del host.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Ritorna 2 se avviato da WSL senza flag, 1 se l'helper non parte."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args(argv)
    if sys.platform != "win32" and not args.allow_non_windows:
        print(
            "Questo host va avviato con python.exe su Windows, non da WSL. "
            "Il loop vocale resta in WSL (AUDIO_DRIVER=http). "
            "Per un import di prova: --allow-non-windows.",
            file=sys.stderr,
        )
        return 2

    helper = HelperSupervisor(
        port=args.helper_port,
        exe=Path(args.helper_exe) if args.helper_exe else None,
        spawn=not args.no_spawn_helper,
    )

    server: AudioHostServer | None = None
    try:
        try:
            helper.ensure_running()
        except RuntimeError as exc:
            _log.error("%s", exc)
            print(str(exc), file=sys.stderr)
            return 1

        # Prima del bind: il primo edge-tts a freddo può durare minuti. Se bindiamo
        # senza serve_forever, WSL in coda su 8765 resta appesa. Warmup qui, poi ascolto.
        try:
            from tts_playback import warmup_synthesis

            warmup_synthesis(voice=args.voice)
        except Exception as exc:  # noqa: BLE001 — import pygame assente nei test WSL
            _log.warning("Warmup TTS saltato: %s", exc)

        state = HostState(
            half_duplex=threading.Lock(),
            speaking=threading.Event(),
            helper_port=args.helper_port,
            listen_forward_timeout=DEFAULT_LISTEN_FORWARD_TIMEOUT,
            tts_voice=args.voice,
            speak_fn=None,
            wake_close=os.environ.get("WAKE_CLOSE") or "assistente chiudi",
            wake_cancel=os.environ.get("WAKE_CANCEL") or "assistente annulla",
        )
        # bind fallisce qui se 8765 è già presa: il finally sotto uccide comunque l'helper.
        server = build_server(bind=args.bind, port=args.port, state=state)
        bound_host, bound_port = server.server_address[:2]
        _log.info(
            "Host audio in ascolto su http://%s:%s (helper 127.0.0.1:%s, voce %s). "
            "Ora puoi lanciare lavora-e-guida da WSL (un solo processo, non rilanciare se Elsa parla).",
            bound_host,
            bound_port,
            args.helper_port,
            args.voice,
        )
        server.serve_forever()
    except KeyboardInterrupt:
        _log.info("Ctrl+C: chiudo il server audio.")
    except OSError as exc:
        _log.error("Bind HTTP fallito: %s", exc)
        print(f"Impossibile ascoltare su {args.bind}:{args.port}: {exc}", file=sys.stderr)
        return 1
    finally:
        if server is not None:
            try:
                server.server_close()
            except OSError:
                pass
        # Anche se il bind è fallito: niente helper orfano su 8766.
        helper.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
