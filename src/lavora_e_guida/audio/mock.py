"""Driver audio Mock: tastiera/CLI al posto di microfono e altoparlanti.

Usato in sviluppo WSL e nei test automatici: nessun socket, nessun WinRT.
In auto (Phase 5) verrà sostituito da `AUDIO_DRIVER=http`; qui Enter sostituisce
la wake phrase / hotkey software prevista lato host.
"""

from __future__ import annotations

import sys
from typing import TextIO

from lavora_e_guida.audio.interface import BaseSTT, BaseTTS


class MockSTT(BaseSTT):
    """STT da stdin: una riga = una “utterance” dell'utente."""

    def __init__(
        self,
        *,
        # Iniettiamo stream e prompt così i test non dipendono da stdin reale.
        prompt: str = "Tu (mock STT)> ",
        infile: TextIO | None = None,
        outfile: TextIO | None = None,
    ) -> None:
        # Prompt solo su stderr/stdout di controllo: non confonde il canale “parlato”.
        self._prompt = prompt
        # Default a sys.stdin/stdout permette lo script manuale senza wiring extra.
        self._infile = infile if infile is not None else sys.stdin
        self._outfile = outfile if outfile is not None else sys.stdout

    def listen(self) -> str:
        # Mostra il prompt prima di bloccare: in auto non c'è UI, qui serve feedback.
        self._outfile.write(self._prompt)
        self._outfile.flush()
        # readline (non input()) rispetta `infile` iniettato nei test.
        line = self._infile.readline()
        # EOF (Ctrl+D / pipe chiusa) → stringa vuota: il loop può uscire in modo soft.
        if line == "":
            return ""
        # Strip solo whitespace di fine riga: spazi iniziali restano (citazioni, ecc.).
        return line.rstrip("\r\n")


class MockTTS(BaseTTS):
    """TTS su stdout: stampa il testo con prefisso riconoscibile."""

    def __init__(
        self,
        *,
        # Prefisso stabile → facile da assertare nei test e da filtrare a occhio.
        prefix: str = "[TTS] ",
        outfile: TextIO | None = None,
    ) -> None:
        self._prefix = prefix
        self._outfile = outfile if outfile is not None else sys.stdout

    def speak(self, text: str) -> None:
        # Una sola riga per utterance: allinea il contratto “frase → feedback”.
        self._outfile.write(f"{self._prefix}{text}\n")
        self._outfile.flush()
