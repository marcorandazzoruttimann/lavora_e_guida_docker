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



## Checklist step


| Step | Descrizione              | OK/KO  | Latenza             | Note qwen / errori tipici                     |
| ---- | ------------------------ | ------ | ------------------- | --------------------------------------------- |
| 0    | Daemon + `qwen2.5:3b`    | **OK** | —                   | Daemon **WSL**; Host Windows non espone 11434 |
| 1    | Loop mock chat (no tool) | ok     | da 50 a 120 secondi | molto lento                                   |
| 2    | `create_text_file`       |        |                     |                                               |
| 3    | `append_note`            |        |                     |                                               |
| 4    | `read_text_file` → TTS   |        |                     |                                               |
| 5    | Riassunto da read        |        |                     |                                               |
| 6    | `read_pdf` (inbox/)      |        |                     |                                               |


Compilare le righe 1–6 a mano dopo ogni validazione: questa tabella decide se il Direct Path del Master può affidarsi a qwen per FS reale.