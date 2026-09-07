# Flowchart: Gmail OAuth e HITL invio

Indice: [flowchart.md](flowchart.md). Operativo: [gmail_oauth.md](gmail_oauth.md). Codice: [`gmail/oauth.py`](../src/lavora_e_guida/gmail/oauth.py), [`gmail/agent.py`](../src/lavora_e_guida/gmail/agent.py), [`gmail/send.py`](../src/lavora_e_guida/gmail/send.py), CLI [`gmail/auth_cli.py`](../src/lavora_e_guida/gmail/auth_cli.py).

Il consenso OAuth **non** parte dal loop vocale. Un enunciato solo non invia: serve HITL sì/no sul loop esterno.

## 1. Consenso a tavolino (`lavora-e-guida-gmail-auth`)

Client OAuth tipo **Desktop**, un solo file token. Runtime chiede `gmail.readonly` + `gmail.send`. `gmail.modify` è dichiarato in Console per il futuro, non nel codice.

```mermaid
flowchart TB
  CLI["lavora-e-guida-gmail-auth"]
  CLI --> FLOW["InstalledAppFlow<br/>open_browser False"]
  FLOW --> LISTEN["listener 127.0.0.1 porta effimera"]
  LISTEN --> URL["stampa URL consenso"]
  URL --> BR["utente apre Chrome/Edge su Windows<br/>account = GMAIL_USER"]
  BR --> REDIR["redirect http://127.0.0.1:porta/code"]
  REDIR --> EX["scambio code → refresh_token"]
  EX --> FILE["scrive INDEX_ROOT/gmail_token.json<br/>permessi 0600"]
  FILE --> PROF["GET users/me/profile"]
  PROF --> MATCH{"emailAddress == GMAIL_USER?"}
  MATCH -->|no| ERR["account sbagliato nel browser"]
  MATCH -->|sì| OK["consenso ok"]
```

Re-auth incrementale: stesso comando, `include_granted_scopes=true`. Google **aggiunge** send senza togliere readonly. Stesso JSON su disco.

Networking: mirrored WSL fa arrivare il redirect al listener Linux. In NAT puro il browser Windows non raggiunge `127.0.0.1` di WSL.

## 2. Token a runtime (mai un browser)

```mermaid
flowchart TB
  GET["get_gmail_credentials"]
  GET --> PATH["resolve_token_path"]
  PATH --> DISK{"gmail_token.json esiste?"}
  DISK -->|no| NL["GmailAuthError<br/>ERRORE: Gmail non collegata"]
  DISK -->|sì| LOAD["Credentials da JSON"]
  LOAD --> SCOPE{"scope coprono readonly + send?"}
  SCOPE -->|no| INS["ERRORE: scope insufficienti<br/>riesegui auth a tavolino"]
  SCOPE -->|sì| REF{"access token scaduto?"}
  REF -->|sì| REFRESH["refresh su oauth2.googleapis.com"]
  REFRESH -->|fail| NL2["GmailAuthError parlante"]
  REFRESH -->|ok| REWRITE["Google riscrive il JSON"]
  REF -->|no| OK["Credentials pronte"]
  REWRITE --> OK
```

Chi chiama il gate:

- `--agent gmail` all’avvio: `_require_gmail_token` → `SystemExit(1)` se manca.
- `--agent master` all’avvio: **niente**. Al primo `ask_gmail`, `gmail_token_is_present` (solo `is_file`, niente refresh). Assenza → `ERRORE:` parlante, nested Gemini non parte.

## 3. Tool vocali mailbox

Gli id messaggio restano in Python. Gemini passa `name` (indice parlato: «la seconda») o `query`.

```mermaid
flowchart TB
  D["dispatch_gmail_tool"]
  D --> WL{"tool in whitelist?"}
  WL -->|no| E0["ERRORE tool sconosciuto"]
  WL -->|list_emails| L["GET messages, query Gmail"]
  WL -->|read_email| R["GET message by name"]
  WL -->|save_attachments| S["scrive email_attachments/YYYY-MM-DD/<br/>non indicizzato da RAG"]
  WL -->|draft_email| DR["bozza in sessione Python<br/>niente REST di invio"]
  WL -->|reply_email / reply_all| RP["bozza reply in sessione"]
  WL -->|send_email| SE["POST users/me/messages/send<br/>solo se HITL ha confermato"]
```

`draft` / `reply` / `reply_all` con `OK:` attivano `gmail_hitl_after_tool`: il TTS parla la conferma (senza prefisso `OK:`) e **salta** Gemini.

## 4. HITL sì / no

```mermaid
flowchart TB
  AFTER["hitl_after_tool su draft/reply OK"]
  AFTER --> SPEAK["tts: destinatario, oggetto, corpo<br/>Di sì per inviare o no per annullare"]
  SPEAK --> LISTEN["stt.listen turno successivo"]
  LISTEN --> ON["gmail_hitl_on_utterance"]
  ON --> WAIT{"session.awaiting_confirm?"}
  WAIT -->|no| GEM["None → enunciato a Gemini"]
  WAIT -->|sì| NORM["casefold, togli punteggiatura<br/>Sì. conta come sì"]

  NORM --> Y{"in sì / ok / conferma / invia ...?"}
  Y -->|sì| CONF["mark_confirmed"]
  CONF --> SEND["send_email REST"]
  SEND --> TTS1["parla esito senza OK:"]

  Y -->|no| N{"in no / annulla / stop ...?"}
  N -->|sì| CLR["session.clear"]
  CLR --> TTS2["Invio annullato."]
  N -->|altro| REP["spoken_draft_confirm di nuovo<br/>stesso corpo, non solo sì o no"]
  REP --> LISTEN
```

Il master inoltra lo stesso interceptor (`gmail_hitl_on_utterance`) e ferma Gemini master con `master_hitl_after_tool` se la sessione è in attesa. Nested Gmail **non** ascolta: l’ascolto resta sul loop esterno.

## 5. Errori HTTP parlanti

401/403 → «Gmail non collegata». 429 → occupata. 400 in send → destinatari. Timeout → non raggiungibile. Niente JSON error Gmail nel TTS.
