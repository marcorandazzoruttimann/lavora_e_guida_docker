# Checkpoint Phase 2.4 — Topologia agenti

## Contesto

Phase 2 introduce:

- state machine `Listening → Thinking → Executing → Speaking`
- classifier intent minimale (`SYSTEM_FILE | CURSOR | WEB_EMAIL | GENERAL`)
- risposte **stub** (nessun tool reale, nessun CrewAI/AutoGen ancora)

Il piano chiede di decidere la topologia **dopo** il benchmark modelli (2.1) e il comportamento del classifier (2.3).

## Vincoli osservati

- WSL tipicamente con **RAM limitata** (~5 Gi in questa macchina di sviluppo): un solo LLM locale alla volta.
- Modelli candidati ultra-compatti: `qwen2.5:3b` (default piano) e `gemma2:2b` (fallback leggero).
- Classifier su 2–3B: utile per etichette grosse, **non** affidabile come planner multi-agente con tool-use complesso.
- Rischio esplicito del piano: *over-splitting* agenti prima che i modelli piccoli dimostrino routing + tool use.

## Esito benchmark 2.1 (questo ambiente)

Dati in `docs/bench_ollama_intent.json` (8 frasi IT di classificazione):

| Modello | Accuracy | Cold | Warm avg |
|---------|----------|------|----------|
| `gemma2:2b` | 8/8 (1.0) | ~108 s | ~34 s |
| `qwen2.5:3b` | 8/8 (1.0) | ~72 s | ~11 s |

**Default `.env`**: resta `OLLAMA_MODEL=qwen2.5:3b` (stessa accuracy, latenza warm migliore). `gemma2:2b` resta fallback se la RAM WSL non regge il 3B.

## Decisione (Phase 2 → ingresso Phase 3)

**Hybrid differito / generalista unico per ora:**

1. **Phase 3.1–3.4**: un solo agente “generalista” dietro `BaseOrchestrator`, con **tools** (FS, Cursor Ask, web leggero) selezionati dal **router intent** già presente. Nessuna crew multi-agente all’avvio.
2. **CrewAI / AutoGen** restano dietro factory lazy (`ORCHESTRATOR_FRAMEWORK`) per confronto didattico e, in seguito, solo su intent “pesanti” (es. CURSOR multi-step) — **non** uno specialist per etichetta fin da subito.
3. **Rivalutare** lo split in agenti specializzati dopo:
   - risultati in `docs/bench_ollama_intent.json` (accuracy + latenza warm);
   - primi test reali tool-use in Phase 3;
   - eventuale policy ibrida cloud in Phase 4.

## Perché non specialisti subito

| Opzione | Pro | Contro in questo hardware |
|---------|-----|---------------------------|
| Un generalista + tools | RAM/processi semplici; router già sufficiente | Prompt tool-use più lungo |
| Uno specialist per dominio | Prompt più stretti | 4× overhead cognitivo/framework; modelli 2–3B non lo giustificano ancora |
| Hybrid (router locale + crew su intent pesanti) | Buon compromesso futuro | Prematuro prima di bench + Phase 3 |

## Criterio di uscita Phase 2 (soddisfatto a livello design)

- Loop classifica + stub TTS: sì (`main.run_agent_loop`).
- Decisione topologia **documentata**: sì — **generalista + tools; specialisti rimandati post Phase 3 / bench**.
