# Sandbox Ollama FS Lab

Lab separato (Phase 3B+ in pausa) per misurare cosa sa fare `qwen2.5:3b` su create/note/read/summary/PDF.

- **Codice**: `sandbox/ollama_fs_lab/` (questo repo)
- **Dati FS**: solo `C:\Users\User\Desktop\Ollama_test` → WSL `/mnt/c/Users/User/Desktop/Ollama_test`
- **LLM**: `LocalOllama` → modello `qwen2.5:3b`
- **I/O**: MockSTT / MockTTS a terminale (dal Step 1)

## Step 0 — Ollama (completato 2026-08-07)


| Check                                  | Esito                                                                     |
| -------------------------------------- | ------------------------------------------------------------------------- |
| Daemon                                 | **WSL** (`/usr/local/bin/ollama serve` su `127.0.0.1:11434`)              |
| Host Windows `:11434`                  | Non raggiungibile (`nameserver` `/etc/resolv.conf` e gateway WSL timeout) |
| `curl http://127.0.0.1:11434/api/tags` | OK                                                                        |
| `LocalOllama.ping()`                   | **True**                                                                  |
| Modello `qwen2.5:3b`                   | Presente (~1.9 GB, Q4_K_M); anche `gemma2:2b`                             |
| Workspace Desktop                      | Cartella `Ollama_test` presente e vuota                                   |


URL e modello fissati in `[config.py](config.py)`: `OLLAMA_URL`, `OLLAMA_MODEL`.

### Ri-verifica rapida

```bash
# Da root del repo, con rete verso il daemon WSL:
curl -sS http://127.0.0.1:11434/api/tags | python3 -m json.tool
ollama list

PYTHONPATH=src python3 -c "
from lavora_e_guida.llm.local_ollama import LocalOllama
c = LocalOllama()
print('ping', c.ping())
print('models', c.list_models())
c.close()
"
```

Se il ping fallisce: avviare il daemon in WSL con `ollama serve` (non confondere con Ollama su Windows).

## Step 1 — Loop mock chat (senza tool)

MockSTT (stdin) → `LocalOllama.chat` (`qwen2.5:3b`) → MockTTS (`[TTS] …`).
Niente tool FS: solo conversazione multi-turno in italiano.
*(Il loop attuale include già lo Step 2; per chat pura basta non chiedere file.)*

```bash
# dalla root del repo (venv attivo consigliato)
PYTHONPATH=src:. python -m sandbox.ollama_fs_lab
```

- Digiti la “frase vocale” dopo `Tu (mock STT)>` .
- La risposta compare come `[TTS] …`; la latenza warm su stderr: `[lab] latenza chat: X.XXs`.
- Uscita: `esci` / `exit` / `quit`, oppure Enter a vuoto / Ctrl+D.

Smoke non interattivo (una domanda + esci):

```bash
printf 'Ciao, rispondi in una frase.\nesci\n' | PYTHONPATH=src:. python -m sandbox.ollama_fs_lab
```



## Step 2 — `create_text_file` sul Desktop

Tool JSON eseguito in Python (niente function-calling Ollama nativo):

- Schema: `{"tool":"create_text_file","args":{"name":"…","content":"…"}}` oppure `{"tool":"none","reply":"…"}`.
- Root: solo `Ollama_test` sul Desktop; path assoluti / `..` → errore parlante.
- All’avvio: `ensure_workspace()` crea `notes/` e `inbox/` se mancano.

Prompt di prova:

> Crea un file chiamato spesa.txt con la lista latte e pane

```bash
printf 'Crea un file chiamato spesa.txt con la lista latte e pane\nesci\n' \
  | PYTHONPATH=src:. python -m sandbox.ollama_fs_lab
```

**Done**: file in `C:\Users\User\Desktop\Ollama_test\spesa.txt`; conferma su `[TTS] …`.

Verifica diretta del tool (senza LLM):

```bash
PYTHONPATH=src:. python -c "
from sandbox.ollama_fs_lab.tools_fs import create_text_file, ensure_workspace
ensure_workspace()
print(create_text_file('spesa.txt', 'latte\\npane'))
"
```



## Step 3 — `append_note` (aggiornamento note)

Tool JSON per aggiungere testo a una nota sotto `notes/` (crea il file se manca):

- Schema: `{"tool":"append_note","args":{"name":"…","content":"…"}}` oppure `{"tool":"none","reply":"…"}`.
- Nome senza cartella (es. `spesa.txt`) → `notes/spesa.txt`; path con cartella resta relativo al root.
- Append: se il file esiste e non termina con newline, ne viene aggiunta una prima del pezzo nuovo.
- Restano attivi anche `create_text_file` e `tool=none`.

Prompt di prova:

> Aggiungi alla nota spesa.txt la riga uova

```bash
printf 'Aggiungi alla nota spesa.txt la riga uova\nesci\n' \
  | PYTHONPATH=src:. python -m sandbox.ollama_fs_lab
```

**Done**: `notes/spesa.txt` aggiornato su Desktop; conferma su `[TTS] …`; riaprire il file su Windows e verificare l’append.

Verifica diretta del tool (senza LLM, due append consecutive):

```bash
PYTHONPATH=src:. python -c "
from pathlib import Path
from sandbox.ollama_fs_lab.config import WORKSPACE_ROOT
from sandbox.ollama_fs_lab.tools_fs import append_note, ensure_workspace
ensure_workspace()
print(append_note('spesa.txt', 'latte\\npane'))
print(append_note('spesa.txt', 'uova'))
print((WORKSPACE_ROOT / 'notes' / 'spesa.txt').read_text(encoding='utf-8'))
"
```



## Step 4 — `read_text_file` → MockTTS

Tool JSON per rileggere un file e far stampare il contenuto via MockTTS:

- Schema: `{"tool":"read_text_file","args":{"name":"…"}}` oppure `{"tool":"none","reply":"…"}`.
- Path risolto in Python (RapidFuzz): basta il nome dal comando (anche senza cartella/estensione).
- Dopo la lettura il modello deve rispondere con `tool=none` ripetendo il contenuto a voce → `[TTS] …`.
- Restano attivi anche `create_text_file` e `append_note`.

Prompt di prova:

> Leggi spesa.txt

```bash
printf 'Leggi spesa.txt\nesci\n' \
  | PYTHONPATH=src:. python -m sandbox.ollama_fs_lab
```

**Done**: stdout `[TTS]` con il contenuto corretto del file (root o `notes/`).

Verifica diretta del tool (senza LLM):

```bash
PYTHONPATH=src:. python -c "
from sandbox.ollama_fs_lab.tools_fs import (
    create_text_file, ensure_workspace, read_text_file,
)
ensure_workspace()
print(create_text_file('spesa.txt', 'latte\\npane'))
print(read_text_file('spesa.txt'))
"
```



## Step 6 — `read_pdf` da `inbox/` → Q&A / riassunto

Tool JSON che estrae testo da un PDF (dipendenza `pypdf`) e lo passa al modello:

- Schema: `{"tool":"read_pdf","args":{"name":"…"}}` oppure `{"tool":"none","reply":"…"}`.
- Copia a mano il PDF in `Ollama_test/inbox/` (Windows Desktop).
- Nome senza cartella (es. `verbale.pdf`) → cerca in `inbox/`, poi in root.
- Testo lungo: tetto ~8000 caratteri (troncamento segnalato nell’esito).
- Dopo l’estrazione: `tool=none` con risposta / riassunto basato sul testo.
- Extra: `pip install pypdf` oppure `pip install -e ".[lab]"` dalla root del repo.

Prompt di prova (dopo aver messo un PDF in `inbox/`):

> Leggi il PDF sample_lab.pdf e riassumilo in italiano in 3 frasi

```bash
# dipendenza Step 6
pip install -e ".[lab]"

printf 'Leggi il PDF sample_lab.pdf e riassumilo in italiano in 3 frasi\nesci\n' \
  | PYTHONPATH=src:. python -m sandbox.ollama_fs_lab
```

**Done**: risposta `[TTS]` basata sul testo estratto dal PDF in `inbox/`.

Verifica diretta del tool (senza LLM):

```bash
PYTHONPATH=src:. python -c "
from sandbox.ollama_fs_lab.tools_fs import ensure_workspace
from sandbox.ollama_fs_lab.tools_pdf import read_pdf
ensure_workspace()
print(read_pdf('sample_lab.pdf')[:500])
"
```



## Checklist step


| Step | Descrizione              | OK/KO  | Latenza             | Note qwen / errori tipici                                                                              |
| ---- | ------------------------ | ------ | ------------------- | ------------------------------------------------------------------------------------------------------ |
| 0    | Daemon + `qwen2.5:3b`    | **OK** | —                   | Daemon **WSL**; Host Windows non espone 11434                                                          |
| 1    | Loop mock chat (no tool) | ok     | da 50 a 120 secondi | molto lento                                                                                            |
| 2    | `create_text_file`       | ok     | 25-30 secondi       | le latenze chat sono doppie per ogni richiesta. tipo 12+14 o 14+16                                     |
| 3    | `append_note`            | ok     | 25-30 secondi       | se gli dici di aggiornare "l'ultimo file" si ricorda il nome ma ne crea uno nuovo nella cartella notes |
| 4    | `read_text_file` → TTS   | ok     | 20 secondi          | sembra ok                                                                                              |
| 5    | Riassunto da read        |        |                     |                                                                                                        |
| 6    | `read_pdf` (inbox/)      |        |                     |                                                                                                        |


Compilare le righe 1–6 a mano dopo ogni validazione: questa tabella decide se il Direct Path del Master può affidarsi a qwen per FS reale.