#!/usr/bin/env python3
"""Benchmark Phase 2.1: qwen2.5:3b vs gemma2:2b su classificazione IT.

Misura latenza (cold + warm), accuratezza su frasi IT di esempio, e stampa
un riepilogo. Non scrive su `.env`: l'operatore sceglie il default dopo.
Uso:
  .venv/bin/python scripts/bench_ollama_intent.py
  .venv/bin/python scripts/bench_ollama_intent.py --models gemma2:2b,qwen2.5:3b
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

# Permette di eseguire lo script senza `pip install -e .` se pythonpath=src.
_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from lavora_e_guida.config import get_settings
from lavora_e_guida.llm.local_ollama import LocalOllama, OllamaError
from lavora_e_guida.routing.intent import IntentClassifier, IntentLabel

# Frasi IT corte: tipiche di comandi vocali in auto (rumore minimo).
_CASES: list[tuple[str, IntentLabel]] = [
    ("elenca i file nella cartella documenti", IntentLabel.SYSTEM_FILE),
    ("copia il file report.pdf sul desktop", IntentLabel.SYSTEM_FILE),
    ("apri il repository e dimmi cosa fa main.py", IntentLabel.CURSOR),
    ("chiedi a Cursor di spiegare questo bug", IntentLabel.CURSOR),
    ("cerca sul web le notizie di oggi", IntentLabel.WEB_EMAIL),
    ("controlla se ho email non lette", IntentLabel.WEB_EMAIL),
    ("che ore sono", IntentLabel.GENERAL),
    ("raccontami una barzelletta corta", IntentLabel.GENERAL),
]


@dataclass
class ModelBench:
    model: str
    available: bool
    cold_latency_s: float | None
    warm_avg_latency_s: float | None
    accuracy: float | None
    correct: int
    total: int
    errors: list[str]
    ram_note: str


def _bench_model(base_url: str, model: str) -> ModelBench:
    """Esegue cold + warm classify su `_CASES` per un singolo modello."""
    llm = LocalOllama(base_url=base_url, model=model, timeout=180.0)
    errors: list[str] = []
    try:
        if not llm.ping():
            return ModelBench(
                model=model,
                available=False,
                cold_latency_s=None,
                warm_avg_latency_s=None,
                accuracy=None,
                correct=0,
                total=len(_CASES),
                errors=["daemon Ollama non raggiungibile"],
                ram_note="n/d",
            )
        installed = llm.list_models()
        # Match tag esatto, prefisso famiglia, o substring (varianti :latest).
        model_present = any(
            m == model or model in m or m.startswith(model.split(":")[0])
            for m in installed
        )
        if not model_present:
            return ModelBench(
                model=model,
                available=False,
                cold_latency_s=None,
                warm_avg_latency_s=None,
                accuracy=None,
                correct=0,
                total=len(_CASES),
                errors=[f"modello non in ollama list: {installed}"],
                ram_note="eseguire: ollama pull " + model,
            )

        classifier = IntentClassifier(llm)
        latencies: list[float] = []
        correct = 0
        for idx, (text, expected) in enumerate(_CASES):
            t0 = time.perf_counter()
            try:
                result = classifier.classify(text)
            except Exception as exc:  # noqa: BLE001 — bench non deve abortire
                errors.append(f"{text!r}: {exc}")
                latencies.append(time.perf_counter() - t0)
                continue
            latencies.append(time.perf_counter() - t0)
            if result.label == expected:
                correct += 1
            else:
                errors.append(
                    f"atteso {expected.value}, ottenuto {result.label.value} "
                    f"(via {result.source}) per {text!r}"
                )
            # Dopo il primo sample consideriamo "warm"; nessuna azione extra.
            _ = idx

        cold = latencies[0] if latencies else None
        warm_vals = latencies[1:] if len(latencies) > 1 else []
        warm_avg = sum(warm_vals) / len(warm_vals) if warm_vals else None
        total = len(_CASES)
        return ModelBench(
            model=model,
            available=True,
            cold_latency_s=round(cold, 3) if cold is not None else None,
            warm_avg_latency_s=round(warm_avg, 3) if warm_avg is not None else None,
            accuracy=round(correct / total, 3) if total else None,
            correct=correct,
            total=total,
            errors=errors,
            ram_note="un solo modello caricato alla volta (unload implicito al cambio)",
        )
    finally:
        llm.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark intent Ollama Phase 2.1")
    parser.add_argument(
        "--models",
        default="gemma2:2b,qwen2.5:3b",
        help="Lista modelli separati da virgola (ordine = ordine di test)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=_ROOT / "docs" / "bench_ollama_intent.json",
        help="Path JSON di output",
    )
    args = parser.parse_args(argv)
    settings = get_settings()
    models = [m.strip() for m in args.models.split(",") if m.strip()]

    print(f"Ollama base URL: {settings.ollama_base_url}")
    results: list[ModelBench] = []
    for model in models:
        print(f"\n=== Benchmark {model} ===")
        bench = _bench_model(settings.ollama_base_url, model)
        results.append(bench)
        print(json.dumps(asdict(bench), ensure_ascii=False, indent=2))

    # Raccomandazione euristica: accuracy primaria, poi latenza warm, poi size.
    available = [r for r in results if r.available and r.accuracy is not None]
    recommendation = None
    if available:
        available.sort(
            key=lambda r: (
                -(r.accuracy or 0.0),
                r.warm_avg_latency_s if r.warm_avg_latency_s is not None else 999.0,
            )
        )
        recommendation = available[0].model

    payload = {
        "base_url": settings.ollama_base_url,
        "results": [asdict(r) for r in results],
        "recommendation": recommendation,
        "note_it": (
            "Aggiornare OLLAMA_MODEL in .env solo dopo review umana. "
            "Con WSL a ~5 Gi RAM preferire gemma2:2b se qwen2.5:3b swap-pa."
        ),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nScritto {args.out}")
    print(f"Raccomandazione automatica: {recommendation}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OllamaError as exc:
        print(f"Errore Ollama: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
