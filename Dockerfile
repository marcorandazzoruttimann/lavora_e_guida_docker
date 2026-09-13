# Immagine dell'orchestratore vocale: Python + pacchetto `lavora_e_guida`.
# Non è un secondo servizio STT/TTS: l'audio resta su Windows (`server.py`).
# Questo file è la ricetta congelata (immagine); il processo vivo è il
# container avviato da Compose o da `docker run`.
#
# Cosa NON entra nel layer (volontà didattica e di privacy):
# - `.env` con chiavi vere: si inietta a runtime (`env_file` / `--env-file`).
# - `runtime/` (token Gmail, Chroma, SQLite): volume `./runtime:/index`.
# - extra `dev` (pytest, ruff): i test restano nel venv WSL.
# Il `.dockerignore` tiene i secret fuori dal *context*; qui non copiamo
# nemmeno i path che il context potrebbe contenere per sbaglio.

# Base ufficiale allineata a `requires-python >= 3.12` in pyproject.toml.
# `slim-bookworm` = Debian 12 senza toolchain di compilazione: wheel PyPI
# bastano per chromadb/onnxruntime su linux/amd64. Se un giorno servisse
# un compilatore, documentarlo in docs/docker.md (non gonfiare la ricetta).
FROM python:3.12-slim-bookworm

# OpenMP runtime: onnxruntime (embedding Chroma DefaultEmbeddingFunction)
# lo carica come .so. Senza `libgomp1` l'import di chromadb esplode a
# runtime con un errore di linker, non in `pip install`. `apt-get update`
# e install nella stessa RUN così il layer non tiene l'indice apt.
# `--no-install-recommends` evita pacchetti Debian extra non chiesti.
# `rm -rf /var/lib/apt/lists/*` pulisce la cache apt nello stesso layer.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Directory di lavoro nel filesystem del container: qui atterrano i COPY
# e da qui parte `pip install`. Non è il workspace utente (`/workspace`
# lo monta Compose) né l'indice (`/index`).
WORKDIR /app

# Solo i file necessari a setuptools per costruire il pacchetto:
# - pyproject.toml = metadati, dipendenze, script `lavora-e-guida`;
# - README.md = campo `readme` del pyproject (pip lo legge in build);
# - src/ = codice. Nessun COPY di `.env`, `docs/`, `tests/`, `runtime/`.
COPY pyproject.toml README.md ./
COPY src/ src/

# Installazione del pacchetto *senza* extra `dev`: nel container gira il
# loop vocale, non pytest. `--no-cache-dir` non lascia wheel in /root/.cache
# (immagine più piccola, niente residui tra rebuild). Il comando `lavora-e-guida`
# finisce sul PATH grazie a `[project.scripts]` nel pyproject.
RUN pip install --no-cache-dir .

# Entrypoint in forma exec (JSON): PID 1 è il CLI, non una shell.
# Gli argomenti dopo l'immagine (es. `docker compose run app --agent fs`)
# si concatenano all'entrypoint. Non usiamo `CMD` di default: il loop
# master è già il default del CLI. Niente shell form (`ENTRYPOINT lavora-e-guida`)
# altrimenti `--agent` non arriverebbe come argv.
ENTRYPOINT ["lavora-e-guida"]
