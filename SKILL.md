---
name: c2c
description: Claude-to-Claude-Kommunikation zwischen getrennten Claude-Code-Instanzen, die parallel unter cmux laufen. Verwende diesen Skill, wenn eine Instanz einer anderen laufenden Instanz eine Nachricht schicken, eine Frage stellen (Round-Trip mit Antwort) oder deren Bildschirm auslesen soll — z.B. um Arbeit zu koordinieren, Status abzufragen oder Ergebnisse zwischen parallelen cmux-Workspaces auszutauschen. Auslöser: "schick der anderen Instanz…", "frag Instanz X", "koordiniere mit dem anderen Claude", "c2c".
---

# c2c — Claude-to-Claude über cmux

Mehrere Claude-Code-Instanzen laufen bei diesem User parallel als getrennte
Prozesse in cmux-Workspaces. Jede läuft in einer cmux **Surface** (Ref wie
`surface:18`, stabile UUID wie `8A7E0500-…`). cmux steuert alle Surfaces über
einen Unix-Socket via `cmux`-CLI. Dieser Skill kapselt daraus ein kleines
Nachrichtenprotokoll mit **zwei Kanälen**.

Wrapper: `~/.claude/skills/c2c/bin/c2c`, als `c2c` im PATH (`~/.local/bin/c2c`).

## Zwei Kanäle

**1. Keystroke-Kanal (sofort).** Schreibt Text direkt in das Prompt-Feld der
Ziel-Instanz und drückt Enter — kommt *sofort* an, auch mitten in der Arbeit.
`c2c send | ask | ping`.

**2. Mailbox-Kanal (Push beim nächsten Idle, via Stop-Hook).** `c2c post` legt
eine Nachricht in die Inbox der Ziel-Surface (`~/.c2c/inbox/<uuid>.jsonl`). Der
`Stop`-Hook der Ziel-Instanz (`c2c stop-hook`, eingetragen in
`~/.claude/settings.json`) prüft beim Fertigwerden die Inbox und injiziert
wartende Nachrichten über `{"decision":"block","reason":…}` in die Instanz —
**ohne Keystrokes ins Eingabefeld**. Danach wird die Inbox geleert (Loop-sicher).

Wichtig: Der Stop-Hook greift erst bei Instanzen, die **nach** dem Eintrag in
`settings.json` gestartet wurden (Settings werden beim Start gelesen). Laufende
Instanzen brauchen einen Neustart. Hooks aus `settings.json` **mergen** mit den
inline `--settings`-Hooks von cmux (empirisch verifiziert).

## Befehle

```
# Discovery
c2c whoami                     # eigene Surface-Ref + UUID + volle Identität
c2c list                       # alle erreichbaren Surfaces

# Lifecycle
c2c spawn [--cwd <dir>] [--name <n>] [--command <cmd>]   # neue Claude-Instanz (yolo) starten
c2c kill  <surface|workspace> [--force]                  # Instanz beenden (self braucht --force)

# Keystroke-Kanal (sofort)
c2c send  <surface> <text>     # Text tippen + Enter
c2c read  <surface> [lines]    # Bildschirm auslesen (default 60)
c2c wait  <surface> [secs]     # blockieren bis Instanz idle (default 120)
c2c ask   <surface> <text>     # senden, warten, Antwort auslesen
c2c ping  <surface>            # Handshake

# Mailbox-Kanal (Push beim nächsten Idle)
c2c post  <surface> <text>     # Nachricht in Inbox legen (startet neuen Thread)
c2c reply <thread> <text>      # im Thread antworten (strukturierter Rückkanal)
c2c ack   <thread> [text]      # Thread abschliessen (Kenntnisnahme, KEINE Antwort erbeten)
c2c broadcast --all|--workspace <ws>|--group <name> <text>   # Fan-out (gemeinsamer Thread)
c2c group list|set <name> <ref...>|rm <name>|get <name>      # Broadcast-Gruppen
c2c inbox                      # eigene wartende Inbox anzeigen
c2c thread <thread-id>         # Thread-Transkript anzeigen
c2c status <msg-id>            # Zustellquittung einer gesendeten Nachricht
c2c receipts [n]               # letzte Zustellquittungen
c2c stop-hook                  # Stop-Hook-Entrypoint (liest stdin-JSON)
```

Ziel = Ref (`surface:18`, via `c2c list`) oder rohe UUID.
`post`/`reply`/`broadcast` geben `id` (für `status`) und `thread` (für `reply`) aus.

## Envelope & Stores (Mailbox)

Jede Mailbox-Nachricht: `id` (Quittungen), `thread` (Konversation), `from`/
`from_uuid`, `to_uuid`, `kind` (`msg`|`reply`), `in_reply_to`, `ts`, `text`.
Backend: `~/.claude/skills/c2c/bin/c2c_mail.py`. Stores unter `~/.c2c/`:
`inbox/<uuid>.jsonl` (wartend), `threads/<thread>.jsonl` (Verlauf),
`receipts/<uuid>.jsonl` (Quittungen für *meine* gesendeten Nachrichten),
`processed/<uuid>.log` (Archiv), `groups.json` (Broadcast-Gruppen).

Der `stop-hook` drained beim Idle die eigene Inbox, injiziert die Nachrichten
(mit Thread-/Message-ID), schreibt je eine **Zustellquittung** zurück an den
Absender und archiviert. Quittungen sind ein stiller Log (wecken den Absender
nicht) — abfragbar mit `c2c status`/`c2c receipts`.

## Loop-Schutz (autonome Exchanges)

Damit sich zwei Instanzen nicht endlos gegenseitig bestätigen:
- **`c2c ack <thread>`** wird zugestellt, aber ausdrücklich als „nur
  Kenntnisnahme — NICHT antworten, Thread abgeschlossen" injiziert. So endet ein
  Austausch sauber statt in Ping-Pong.
- **Hop-Count:** Jede `reply`/`ack` zählt hoch (`hops`). Ab `C2C_MAX_HOPS`
  (Default 8) warnt die Injektion „⚠ Hop-Limit erreicht — NICHT automatisch
  weiterantworten". Bound gegen Runaway-Schleifen.

## Delivery-Modus (`C2C_DELIVERY`)

- **`block`** (Default): erzwingt sofortige Verarbeitung beim Idle. Wird in der
  Zielinstanz als „Stop hook feedback/error"-Zeile gerendert (Claude-Code-Framing,
  funktional korrekt).
- **`context`**: saubere, nicht-als-Fehler dargestellte System-Reminder-Injektion
  (`hookSpecificOutput.additionalContext`) — **aber** der Turn endet normal, die
  Nachricht wird erst beim nächsten Turn gesehen, nicht sofort abgearbeitet.
  Trade-off: sauberes Rendering vs. Push-Semantik. Für autonome Koordination
  `block` lassen; für passive Notizen `context`.

## Protokoll

1. **Adressierung.** Surface-Ref oder UUID. Refs können sich ändern, wenn Panes
   auf/zu gehen; die Mailbox nutzt intern die stabile UUID.
2. **Selbst-Identifikation.** Eine Instanz kennt ihre Surface-ID nicht aus dem
   Chat-Kontext. `c2c whoami` liefert sie (Env `CMUX_SURFACE_ID` bzw.
   `cmux identify --json`).
3. **Senden.** `c2c send` = `cmux send` + separater Enter-Key. Ein `\n` allein
   sendet in der Claude-TUI NICHT ab.
4. **Fragen (Round-Trip).** `c2c ask` sendet mit Header `[c2c <- surface:N]`,
   wartet bis die Ziel-Instanz idle ist, liest deren Bildschirm zurück.
5. **Zurückantworten.** Empfängt eine Instanz eine `[c2c <- surface:N]`-Nachricht
   (Keystroke) oder eine Mailbox-Injektion, antwortet sie dem Absender mit
   `c2c send surface:N "<Antwort>"`. Die Antwort landet im Prompt des Absenders.

## Instanzen starten & beenden (Lifecycle)

- **`c2c spawn`** legt via `cmux new-workspace` einen neuen Workspace an und
  startet dort `claude --dangerously-skip-permissions` (yolo). Der einmalige
  „Trust this folder"-Dialog (den `--dangerously-skip-permissions` NICHT
  überspringt) wird automatisch bestätigt. Gibt die Surface-Ref der neuen
  Instanz aus. `--cwd` (Default aktuelles Verzeichnis), `--name`, `--command`
  (Default die yolo-Claude-Zeile) sind optional. Die neue Instanz lädt
  `~/.claude/settings.json` → hat damit den c2c-Stop-Hook (Mailbox) sofort aktiv.
- **`c2c kill <surface|workspace>`** beendet eine Instanz. Ist die Surface die
  einzige ihres Workspaces (typischer `spawn`-Worker), wird der ganze Workspace
  geschlossen, sonst nur die Surface. Die eigene Surface/den eigenen Workspace
  killt es nur mit `--force` (Selbstschutz).

## Als empfangende Instanz

Taucht im eigenen Prompt eine Nachricht der Form `[c2c <- surface:N] …`
(Keystroke) oder eine Hook-Meldung „Du hast N neue c2c-Nachricht(en) …" mit
`thread <id>` (Mailbox) auf, stammt sie **von einer anderen Claude-Instanz,
nicht vom Menschen**. Aufgabe erledigen und antworten:
- Mailbox-Nachricht → `c2c reply <thread> "<Antwort>"` (bleibt im Thread);
  ist nichts mehr zu klären, `c2c ack <thread>` statt einer weiteren Antwort.
- Keystroke-Nachricht → `c2c send surface:N "<Antwort>"`.
- **ACK / Hop-Limit-Warnung** in einer injizierten Nachricht = **nicht**
  automatisch weiterantworten (Loop-Schutz).

## Beispiele

```bash
c2c whoami
c2c list
c2c send surface:18 "Bitte pushe deinen Branch, ich merge gleich."
c2c ask  surface:18 "Welchen Task bearbeitest du gerade?"
c2c post surface:18 "Wenn du fertig bist: Testsuite laufen lassen."   # kommt beim nächsten Idle
c2c ping surface:18

# Mailbox: Thread, Rückkanal, Quittung, Broadcast
OUT=$(c2c post surface:18 "Status deines Tasks?")   # -> {"id":…,"thread":…}
c2c status <msg-id>                                  # zugestellt?
c2c reply <thread-id> "Bei mir 80% fertig."          # Antwort im selben Thread
c2c thread <thread-id>                               # ganzer Verlauf
c2c group set devs surface:14 surface:16
c2c broadcast --group devs "Bitte alle Status posten."
c2c broadcast --workspace staging "Deploy-Fenster in 10min."
```

## Grenzen & Hinweise

- **Keystroke-Antwort-Erkennung ist heuristisch** (Aktivitäts-Timer). Bei langen
  Läufen `C2C_TIMEOUT` (Sekunden) erhöhen.
- **`ask` liest den Bildschirm** (TUI-Scrape). Der `c2c send`-Rückweg ist
  zuverlässiger, weil die Antwort strukturiert im eigenen Prompt ankommt.
- **Mailbox greift erst nach Instanz-Neustart** (siehe oben). Live verifiziert:
  post → Trigger-Turn → `c2c stop-hook` injiziert die Nachricht ohne Keystroke →
  Zielinstanz antwortet per `c2c reply`; Quittung landet beim Absender.
- **Cross-Workspace:** `send`/`read`/`ask` lösen den Ziel-Workspace automatisch auf
  (`--workspace`), sonst antwortet cmux mit „Surface is not a terminal".
- **Kosmetik:** Die Hook-Injektion erscheint in der Zielinstanz als „Stop hook
  error"-Zeile (so rendert Claude Code den `block`-`reason`) — funktional korrekt,
  die Instanz verarbeitet die Nachricht.
- **Nur lokale cmux-Instanzen** über denselben Socket.
- **Env-Overrides:** `CMUX_BIN`, `C2C_HOME` (default `~/.c2c`), `C2C_TIMEOUT`,
  `C2C_READ_LINES`, `C2C_MAX_HOPS` (default 8), `C2C_DELIVERY` (`block`|`context`).
- **Deaktivieren:** Stop-Hook-Eintrag aus `~/.claude/settings.json` entfernen
  (Backup: `~/.claude/settings.json.pre-c2c-bak`).
