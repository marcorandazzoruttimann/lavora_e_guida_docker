# Flowchart: workspace, file e RAG

Indice: [flowchart.md](flowchart.md). Codice: [`fs/files.py`](../src/lavora_e_guida/fs/files.py), [`fs/find.py`](../src/lavora_e_guida/fs/find.py), [`fs/file_resolver.py`](../src/lavora_e_guida/fs/file_resolver.py), [`rag/index_sync.py`](../src/lavora_e_guida/rag/index_sync.py).

Collection Chroma **unica** sotto `INDEX_ROOT`. I path in SQLite sono relativi al data workspace. `email_attachments/` è skip: fatture scaricate non mescolano le note in `find_file`.

## 1. Due root all’avvio (master e `--agent fs`)

```mermaid
flowchart TB
  PRE["_prepare_workspace"]
  PRE --> ENS["ensure_workspace"]
  ENS --> ROOT["WORKSPACE_ROOT montato"]
  ROOT --> DIRS["crea notes/ e inbox/ se mancano"]
  DIRS -->|OSError| FAIL["SystemExit 1 Desktop non accessibile"]
  DIRS --> SYNC["sync_workspace_index"]
  SYNC --> IDX["INDEX_ROOT/runtime<br/>files.db + chroma/"]
  SYNC -->|ImportError chromadb| WARN["stderr: RAG non disponibile"]
  SYNC -->|OSError| WARN2["stderr: sync fallita"]
  IDX --> BANNER["banner data= index="]
```

Gmail e web isolati **non** passano da qui.

## 2. Sync full all’avvio

```mermaid
flowchart TB
  SYNC["sync_workspace_index"]
  SYNC --> WALK["iter_indexable_files<br/>rglob sotto workspace"]
  WALK --> SKIP{"primo segmento in<br/>INDEX_SKIP_DIRNAMES?"}
  SKIP -->|sì, email_attachments| NEXT["non indicizzare"]
  SKIP -->|no| HASH["hash contenuto vs SQLite"]
  HASH -->|invariato| SKIPU["skipped_unchanged"]
  HASH -->|nuovo o modificato| CHUNK["chunking"]
  CHUNK --> UPS["upsert Chroma + riga SQLite"]
  UPS --> NEXT2["prossimo file"]
  NEXT --> NEXT2
  SKIPU --> NEXT2
  NEXT2 --> ORPH["path in DB ma non su disco"]
  ORPH --> DEL["delete Chroma + SQLite"]
```

Statistiche su stderr: `scanned`, `upserted`, `skipped_unchanged`, `deleted`. Un file corrotto non ferma il batch: va in `errors`.

## 3. Tool FS: path risolto in Python

Gemini passa `name` o `query`, mai un path assoluto. Il resolver (RapidFuzz) resta dentro `WORKSPACE_ROOT`.

```mermaid
flowchart TB
  D["dispatch_fs_tool"]
  D --> WL{"whitelist?"}
  WL -->|no| E0["ERRORE tool sconosciuto"]
  WL -->|find_file| Q{"query non vuota?"}
  Q -->|no| E1["ERRORE args.query"]
  Q -->|sì| FIND["find_file → Chroma query"]
  FIND --> OKR["OK: header + chunk<br/>print RAG"]

  WL -->|read_file| N1{"name non vuoto?"}
  N1 -->|no| E2["ERRORE args.name"]
  N1 -->|sì| READ["resolve + leggi testo o PDF"]
  READ --> OKF["OK + corpo, print FS"]

  WL -->|create_text_file| CR["crea in notes/ o inbox/"]
  WL -->|append_note| AP["append, crea se manca"]
  CR --> HOOK["upsert_indexed_file quel path"]
  AP --> HOOK
  HOOK --> OKW["OK parlante"]
```

Traversal / file assente → `FsToolError` già in italiano → `ERRORE:` verso Gemini. Disco pieno / permessi WSL→Windows → `ERRORE I/O`, niente traceback nel parlato.

## 4. Hook post-scrittura vs skip allegati

```mermaid
flowchart TB
  WRITE["create_text_file o append_note"]
  WRITE --> REL["rel_path posix"]
  REL --> SKIP{"is_index_skipped_rel?"}
  SKIP -->|sì| NO["return False, niente Chroma"]
  SKIP -->|no| SAFE{"assoluto o .. nei parts?"}
  SAFE -->|sì| VAL["ValueError path non sicuro"]
  SAFE -->|no| ABS["resolve sotto workspace"]
  ABS --> UPS["chunk + upsert"]
```

`save_attachments` Gmail scrive sotto `email_attachments/`: anche un hook successivo non indicizza. `notes/email_attachments.txt` resta una nota visibile (lo skip è solo il **primo** segmento del path).

## 5. `find_file` a voce

```mermaid
flowchart TB
  Q["query in italiano"]
  Q --> EMB["embedding query"]
  EMB --> COL["collection unica Chroma"]
  COL --> TOP["più chunk + path"]
  TOP --> SPOKEN["OK: trovato in nota spesa..."]
  SPOKEN --> GEM["Gemini conferma a voce<br/>senza inventare path"]
```

Puntualità: path + più chunk all’agente, non indici separati «note vs allegati». Se Chroma manca all’avvio, `find_file` risponde con errore parlante, gli altri tool FS (create/read/append) restano usabili.
