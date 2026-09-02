"""Agente master: router vocale, tre tool di delega verso gli specialisti.

Non esegue i tool di dominio (file, mailbox, Tavily): smista con `ask_fs`,
`ask_gmail` e `ask_web` a uno specialista che ha storia isolata, poi riporta
l'esito parlante al loop (unico possessore di STT/TTS). Lo spec vocale è in
`master.agent`.
"""