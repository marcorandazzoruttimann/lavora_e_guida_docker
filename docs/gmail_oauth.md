# OAuth Gmail (connessione a tavolino)

Collega un account Gmail personale (`GMAIL_USER`) alla Gmail API REST. Il consenso OAuth è un’operazione **da scrivania, una tantum**: non parte dal loop vocale `lavora-e-guida` e non entra nel system prompt del modello 3B.

- **API:** Gmail REST, non IMAP.
- **Client:** un solo ID OAuth tipo **Desktop**, un solo file token.
- **Primo consenso (fase 1):** solo `gmail.readonly`. Invio e modifica mailbox **non** si chiedono nel browser finché l’app non li usa.
- **Console:** dichiarare **ora** anche `gmail.send` e `gmail.modify` sulla schermata consenso, così aggiungere quei permessi in seguito è un re-consenso incrementale, non una seconda integrazione.

I tool vocali (`list_emails` / `read_email`, poi send/modify) arrivano dopo e riusano lo stesso `gmail_token.json`.

## 1. Checklist Google Cloud Console (una tantum)

Il codice non può creare il progetto né le credenziali. Fare questi passi a mano, una volta.

1. Aprire [Google Cloud Console](https://console.cloud.google.com/) e scegliere un progetto (nuovo o esistente).
2. **API e servizi** → **Libreria** → abilitare **Gmail API**.
3. **Schermata consenso OAuth**:
   - Tipo utente: **Esterno**.
   - Stato pubblicazione: **Testing** (non “In produzione”: gli scope Gmail sono sensibili e in Testing funzionano solo per gli utenti di test).
   - **Utenti di test:** aggiungere l’indirizzo esatto di `GMAIL_USER` (es. `account@gmail.com`). Senza questo il login fallisce anche con client id/secret corretti.
4. **Scope** sulla stessa schermata: dichiararli **tutti e tre ora**, così non si torna in Console quando arriveranno invio e modifica:
   - `https://www.googleapis.com/auth/gmail.readonly` — lettura mailbox
   - `https://www.googleapis.com/auth/gmail.send` — invio (fase futura)
   - `https://www.googleapis.com/auth/gmail.modify` — etichette, archivio, cestino, letto/non letto (fase futura)
5. **Credenziali** → **Crea credenziali** → **ID client OAuth 2.0** → tipo applicazione **Desktop**.
6. Copiare **Client ID** e **Client secret** in `.env` come `GMAIL_CLIENT_ID` e `GMAIL_CLIENT_SECRET`. Impostare anche `GMAIL_USER` all’indirizzo dell’utente di test. Non committare il JSON scaricato da Google (`client_secret*.json`).

Dichiarare gli scope in Console **non** è lo stesso che richiederli nel flusso OAuth. Il primo `lavora-e-guida-gmail-auth` chiede solo readonly; send e modify restano in Console pronti per un consenso successivo.

## 2. Perché Desktop (non Web, non Service Account)

| Tipo client | Usarlo? | Motivo |
| --- | --- | --- |
| **Desktop** | Sì | Flusso con redirect su `http://127.0.0.1` e listener locale; adatto a CLI su WSL2. |
| Web | No | Serve un redirect URI di un’app web pubblica; non è questo progetto. |
| Service Account | No | Non apre una mailbox Gmail **personale** (`users/me`). |

Un solo client Desktop per tutto: lettura ora, send/modify dopo. Non creare un secondo client né un secondo account.

## 3. Dove vivono i secret

| Cosa | Dove | Note |
| --- | --- | --- |
| Client ID / secret | `.env` (già gitignored) | `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET` |
| Account atteso | `.env` | `GMAIL_USER=account@gmail.com` |
| Refresh token | `INDEX_ROOT/gmail_token.json` | Default: `runtime/gmail_token.json`. Google **riscrive** il JSON al refresh: non metterlo nel `.env`. |
| Path token opzionale | `.env` | `# GMAIL_TOKEN_FILE=` — se omesso, `INDEX_ROOT/gmail_token.json` |

Il file token ha permessi restrittivi (`0600`). Non va in git.

## 4. Primo consenso (solo lettura) su WSL2

Dalla root del repo, venv attivo:

```bash
lavora-e-guida-gmail-auth
```

Il comando non tocca STT, TTS né LLM. Non va lanciato in auto.

Flusso atteso:

1. Lo script avvia un listener su `127.0.0.1` **senza** aprire il browser da solo (`open_browser=False`).
2. Stampa l’URL di consenso: aprirlo **nel browser Windows** (Chrome/Edge), non in un browser headless WSL.
3. Accedere con l’account `GMAIL_USER` (lo stesso utente di test in Console).
4. Google reindirizza a `http://127.0.0.1:<porta>/...` con il `code`; il listener in WSL lo scambia con un `refresh_token` e scrive `gmail_token.json`.
5. Lo script chiama `GET https://gmail.googleapis.com/gmail/v1/users/me/profile`. Se `emailAddress` è diverso da `GMAIL_USER`, errore chiaro (account sbagliato nel browser).

Nella schermata Google deve comparire il permesso di **lettura**. Non deve comparire “inviare email a tuo nome”: send/modify non sono nel primo consenso.

### Networking WSL2 e localhost

Con **networking mirrored** di WSL2, il redirect `http://127.0.0.1` dal browser Windows torna al listener in Linux. È la configurazione prevista.

Se WSL è in modalità **NAT** (o una versione vecchia che non inoltra il loopback Windows → WSL):

- il browser apre l’URL, il login Google funziona, ma il redirect su `127.0.0.1` **non** raggiunge il processo Python in WSL;
- in quel caso abilitare il mirrored mode, oppure inoltrare esplicitamente la porta verso WSL, poi rilanciare `lavora-e-guida-gmail-auth`.

## 5. Re-auth incrementale (send / modify, più avanti)

Quando servirà scrivere in mailbox **non** si crea un nuovo client OAuth.

1. In Console gli scope `gmail.send` e `gmail.modify` sono già dichiarati (passo 4 della checklist).
2. Nel codice si aggiungono `SCOPE_SEND` e `SCOPE_MODIFY` alla lista runtime `GMAIL_SCOPES` (oggi è solo readonly).
3. A tavolino si rilancia `lavora-e-guida-gmail-auth`. Il flusso usa `include_granted_scopes=True`: Google **aggiunge** i nuovi permessi senza togliere readonly. Stesso `gmail_token.json`.
4. I tool vocali di invio / marca-letto arriveranno dopo, sempre un tool per turno, con messaggi `OK:` / `ERRORE:` in italiano. Prima di un send in guida servirà una conferma esplicita (HITL), da definire in quel piano.

Se il token su disco esiste ma **non copre** gli scope che il codice chiede (es. solo readonly mentre il caller vuole anche send): errore parlante del tipo “scope insufficienti, riesegui autenticazione a tavolino”. Il loop vocale **non** apre il browser.

## 6. Loop vocale e assenza di token

`lavora-e-guida` non avvia mai OAuth. Se il token manca, il refresh fallisce o gli scope non bastano, i futuri tool email rispondono in italiano, ad esempio:

`ERRORE: Gmail non collegata, esegui autenticazione a tavolino`

Poi si torna alla checklist di questa pagina e al comando `lavora-e-guida-gmail-auth`.
