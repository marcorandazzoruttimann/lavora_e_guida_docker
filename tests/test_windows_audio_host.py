"""Contratto HTTP del host Windows (senza pygame, senza WinRT, senza edge-tts).

I moduli stanno in ``windows_audio/`` (venv host separato). Qui li importiamo
aggiungendo quella cartella al path: pytest WSL resta su ``tests/`` e non
chiede le dipendenze di ``requirements.txt``.
"""

from __future__ import annotations

import http.server
import socket
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest

# windows_audio/ è accanto a tests/, fuori da src/: non è un package installato.
_HOST_DIR = Path(__file__).resolve().parents[1] / "windows_audio"
if str(_HOST_DIR) not in sys.path:
    sys.path.insert(0, str(_HOST_DIR))

from helper_spawn import HelperSupervisor, _sources_newer_than, find_helper_exe
from server import (
    DEFAULT_VOICE,
    HostState,
    _transcript_from_helper_body,
    build_server,
    main,
    parse_args,
)


class _FakeHelperHandler(http.server.BaseHTTPRequestHandler):
    """Stand-in dell'helper C# :8766. Solo /health e /listen, niente WinRT."""

    def log_message(self, fmt: str, *args: object) -> None:
        # Test: niente access log sulla console pytest.
        return

    def do_GET(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path in ("/health", "/"):
            self._send(200, b'{"ok":true}')
            return
        self._send(404, b"{}")

    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/")
        if path != "/listen":
            self._send(404, b"{}")
            return
        started = getattr(self.server, "listen_started", None)
        if started is not None:
            started.set()
        delay = float(getattr(self.server, "listen_delay", 0.0))
        if delay > 0:
            time.sleep(delay)
        body = getattr(
            self.server,
            "listen_body",
            b'{"transcript":"ciao dal helper"}',
        )
        status = int(getattr(self.server, "listen_status", 200))
        self._send(status, body)

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _ThreadedServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    listen_delay: float = 0.0
    listen_body: bytes = b'{"transcript":"ciao dal helper"}'
    listen_status: int = 200
    listen_started: threading.Event | None = None


@contextmanager
def _running(httpd: http.server.HTTPServer) -> Iterator[tuple[str, int]]:
    """Avvia ``serve_forever`` in un daemon e smonta in teardown."""
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    try:
        yield str(host), int(port)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2.0)


@pytest.fixture
def fake_helper() -> Iterator[tuple[str, int, _ThreadedServer]]:
    """Helper loopback su porta effimera: il host Python inoltra qui."""
    httpd = _ThreadedServer(("127.0.0.1", 0), _FakeHelperHandler)
    with _running(httpd) as (host, port):
        yield host, port, httpd


def _host_state(helper_port: int, speak_fn) -> HostState:
    return HostState(
        half_duplex=threading.Lock(),
        speaking=threading.Event(),
        helper_port=helper_port,
        listen_forward_timeout=5.0,
        tts_voice=DEFAULT_VOICE,
        speak_fn=speak_fn,
    )


@pytest.fixture
def audio_host(
    fake_helper: tuple[str, int, _ThreadedServer],
) -> Iterator[tuple[str, list[tuple[str, str]]]]:
    """Host Python su 127.0.0.1:0 con speak_fn che registra le chiamate."""
    _helper_host, helper_port, _httpd = fake_helper
    spoken: list[tuple[str, str]] = []

    def speak_fn(text: str, voice: str) -> None:
        spoken.append((text, voice))

    state = _host_state(helper_port, speak_fn)
    httpd = build_server(bind="127.0.0.1", port=0, state=state)
    with _running(httpd) as (host, port):
        yield f"http://{host}:{port}", spoken


def test_main_fail_fast_fuori_windows() -> None:
    """Da WSL, senza --allow-non-windows, non spawn e non apre 8765."""
    if sys.platform == "win32":
        pytest.skip("fail-fast solo fuori da Windows")
    assert main(["--port", "1"]) == 2


def test_parse_args_default_voce_e_porte(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default allineati al piano: 8765, helper 8766, Elsa."""
    for name in ("AUDIO_HOST_PORT", "AUDIO_HOST_BIND", "STT_HELPER_PORT", "TTS_VOICE"):
        monkeypatch.delenv(name, raising=False)
    args = parse_args([])
    assert args.port == 8765
    assert args.helper_port == 8766
    assert args.voice == "it-IT-ElsaNeural"
    assert args.bind == "0.0.0.0"
    assert args.no_spawn_helper is False


def test_transcript_helper_rifiuta_vuoto() -> None:
    """Stringa vuota o JSON senza chiave → None: il handler farà 502, non 200."""
    assert _transcript_from_helper_body(b'{"transcript":"ciao"}') == "ciao"
    assert _transcript_from_helper_body(b'{"transcript":"  "}') is None
    assert _transcript_from_helper_body(b'{"transcript":""}') is None
    assert _transcript_from_helper_body(b"{}") is None
    assert _transcript_from_helper_body(b"not-json") is None


def test_find_helper_exe_preferisce_mtime(tmp_path: Path) -> None:
    """Due exe sotto bin/: vince quello toccato per ultimo (Release vs Debug)."""
    older = tmp_path / "bin" / "Debug" / "stt_helper.exe"
    newer = tmp_path / "bin" / "Release" / "stt_helper.exe"
    older.parent.mkdir(parents=True)
    newer.parent.mkdir(parents=True)
    older.write_bytes(b"old")
    time.sleep(0.05)
    newer.write_bytes(b"new")
    found = find_helper_exe(tmp_path)
    assert found == newer
    assert find_helper_exe(tmp_path / "missing") is None


def test_sources_newer_than_rileva_cs_modificato(tmp_path: Path) -> None:
    """Patch a un .cs deve far scartare l'exe in bin/ al prossimo spawn."""
    exe = tmp_path / "bin" / "stt_helper.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"old")
    time.sleep(0.05)
    (tmp_path / "Program.cs").write_text("// nuovo", encoding="utf-8")
    assert _sources_newer_than(exe, tmp_path) is True
    # Exe toccato dopo i sorgenti: non è stantio.
    time.sleep(0.05)
    exe.write_bytes(b"rebuilt")
    assert _sources_newer_than(exe, tmp_path) is False


def test_supervisor_riusa_helper_gia_pronto(
    fake_helper: tuple[str, int, _ThreadedServer],
) -> None:
    """Helper già in /health: non spawnare un secondo processo sulla stessa porta."""
    _host, port, _httpd = fake_helper
    supervisor = HelperSupervisor(
        port=port,
        spawn=True,
        project_dir=Path("/nonexistent-stt-helper"),
    )
    supervisor.ensure_running()
    assert supervisor.owned_proc is None


def test_health_ok_con_helper(audio_host: tuple[str, list[tuple[str, str]]]) -> None:
    """GET /health 200 solo se l'helper risponde: readiness dello stack, non solo Python."""
    base, _spoken = audio_host
    with httpx.Client(timeout=2.0) as client:
        resp = client.get(f"{base}/health")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is True
    assert payload["helper"] is True
    assert payload["speaking"] is False


def test_health_503_senza_helper() -> None:
    """Helper spento → 503. WSL/curl -f capiscono che lo stack non è pronto."""
    closed = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    closed.bind(("127.0.0.1", 0))
    dead_port = closed.getsockname()[1]
    closed.close()

    def boom(text: str, voice: str) -> None:
        raise AssertionError("speak non deve partire da /health")

    state = _host_state(dead_port, boom)
    httpd = build_server(bind="127.0.0.1", port=0, state=state)
    with _running(httpd) as (host, port):
        with httpx.Client(timeout=2.0) as client:
            resp = client.get(f"http://{host}:{port}/health")
        assert resp.status_code == 503
        assert resp.json()["helper"] is False
        assert resp.json()["ok"] is False


def test_listen_inoltra_transcript(
    audio_host: tuple[str, list[tuple[str, str]]],
) -> None:
    """POST /listen → JSON helper, stessa chiave del client WSL HttpBridgeSTT."""
    base, _spoken = audio_host
    with httpx.Client(timeout=3.0) as client:
        resp = client.post(f"{base}/listen")
    assert resp.status_code == 200
    assert resp.json()["transcript"] == "ciao dal helper"


def test_listen_awake_poi_dictate_fn(
    fake_helper: tuple[str, int, _ThreadedServer],
) -> None:
    """Helper ``awake``: il host non inoltra transcript vuoto, chiama dictate_fn."""
    _host, helper_port, helper_httpd = fake_helper
    helper_httpd.listen_body = b'{"awake":true}'

    class _Outcome:
        kind = "close"
        text = "aggiungi latte alla spesa"

    state = _host_state(helper_port, lambda text, voice: None)
    state.dictate_fn = lambda _stop: _Outcome()
    httpd = build_server(bind="127.0.0.1", port=0, state=state)
    with _running(httpd) as (host, port):
        with httpx.Client(timeout=3.0) as client:
            resp = client.post(f"http://{host}:{port}/listen")
        assert resp.status_code == 200
        assert resp.json()["transcript"] == "aggiungi latte alla spesa"


def test_listen_transcript_vuoto_diventa_502(
    fake_helper: tuple[str, int, _ThreadedServer],
) -> None:
    """Helper 200 con transcript vuoto non deve far uscire il loop WSL."""
    _host, helper_port, helper_httpd = fake_helper
    helper_httpd.listen_body = b'{"transcript":""}'
    state = _host_state(helper_port, lambda text, voice: None)
    httpd = build_server(bind="127.0.0.1", port=0, state=state)
    with _running(httpd) as (host, port):
        with httpx.Client(timeout=3.0) as client:
            resp = client.post(f"http://{host}:{port}/listen")
        assert resp.status_code == 502
        assert "transcript" not in resp.json() or resp.json().get("transcript") in (
            None,
            "",
        )


def test_speak_chiama_playback_e_200(
    audio_host: tuple[str, list[tuple[str, str]]],
) -> None:
    """POST /speak {text} → speak_fn, 200 solo dopo (qui speak_fn è sincrono)."""
    base, spoken = audio_host
    with httpx.Client(timeout=3.0) as client:
        resp = client.post(f"{base}/speak", json={"text": "Ciao dal tts"})
    assert resp.status_code == 200
    assert spoken == [("Ciao dal tts", "it-IT-ElsaNeural")]


def test_speak_testo_vuoto_400(
    audio_host: tuple[str, list[tuple[str, str]]],
) -> None:
    """text mancante o whitespace: 400, playback non parte."""
    base, spoken = audio_host
    with httpx.Client(timeout=2.0) as client:
        missing = client.post(f"{base}/speak", json={})
        blank = client.post(f"{base}/speak", json={"text": "   "})
        bad = client.post(f"{base}/speak", content=b"not-json")
    assert missing.status_code == 400
    assert blank.status_code == 400
    assert bad.status_code == 400
    assert spoken == []


def test_mutex_listen_aspetta_fine_speak(
    fake_helper: tuple[str, int, _ThreadedServer],
) -> None:
    """Half-duplex: /listen non parte verso l'helper finché il TTS tiene il lock."""
    _h, helper_port, helper_httpd = fake_helper
    hold = threading.Event()
    speak_started = threading.Event()
    listen_reached_helper = threading.Event()
    helper_httpd.listen_started = listen_reached_helper

    def speak_fn(text: str, voice: str) -> None:
        speak_started.set()
        assert hold.wait(timeout=2.0)

    state = _host_state(helper_port, speak_fn)
    httpd = build_server(bind="127.0.0.1", port=0, state=state)
    with _running(httpd) as (host, port):
        base = f"http://{host}:{port}"
        speak_box: dict[str, httpx.Response] = {}
        listen_box: dict[str, httpx.Response] = {}

        def do_speak() -> None:
            with httpx.Client(timeout=5.0) as client:
                speak_box["r"] = client.post(f"{base}/speak", json={"text": "attendi"})

        def do_listen() -> None:
            with httpx.Client(timeout=5.0) as client:
                listen_box["r"] = client.post(f"{base}/listen")

        speaker = threading.Thread(target=do_speak)
        speaker.start()
        assert speak_started.wait(timeout=2.0)

        listener = threading.Thread(target=do_listen)
        listener.start()
        # Mentre Elsa parla l'helper NON deve vedere /listen.
        time.sleep(0.3)
        assert not listen_reached_helper.is_set()

        # /health non prende il mutex: deve rispondere durante lo speak.
        with httpx.Client(timeout=2.0) as client:
            health = client.get(f"{base}/health")
        assert health.status_code == 200
        assert health.json()["speaking"] is True

        hold.set()
        speaker.join(timeout=3.0)
        listener.join(timeout=3.0)
        assert speak_box["r"].status_code == 200
        assert listen_box["r"].status_code == 200
        assert listen_box["r"].json()["transcript"] == "ciao dal helper"
        assert listen_reached_helper.is_set()
