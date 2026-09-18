# Affinity beobachten und projektlokal aktivieren

Dieser Pfad erzeugt eine **Observation**. Er erteilt keine
Mutationsberechtigung.

## Ablauf

1. **Inventar.** Löse den kanonischen Projektroot und den aktiven Agent-Harness
   auf. Inventarisiere dessen sichtbare MCP-Server vollständig. Der Schritt ist
   abgeschlossen, wenn null, genau ein oder mehrere native Affinity-Angebote
   belegt sind; eine unvollständige Inventur bleibt `unknown`.
2. **Probe.** Prüfe jeden beobachteten IPv4-/IPv6-Loopback-Kandidaten auf eine
   erfolgreiche SSE-Initialisierung. Lies `preamble`, ermittle Servername,
   Version, Toolnamen und Hashes der Eingabeschemas. Der Schritt ist
   abgeschlossen, wenn genau ein Kandidat `available` ist oder alle Kandidaten
   geschlossen `unavailable|ambiguous|unknown` klassifiziert sind.
3. **Aktivierung.** Führe diesen Schritt nur aus, wenn der Auftrag die
   projektlokale Einrichtung umfasst:

   - Claude Code: Merge den Server als Typ `sse` in
     `<project-root>/.mcp.json`; erhalte vorhandene Einträge und lasse die
     Projektfreigabe sichtbar bestätigen.
   - Codex: Verwende `bin/codex-brandkit`. Der Launcher injiziert die gepinnte
     `toolchain/affinity-codex/affinity-codex-bridge.mjs` für diesen Prozess.
   - Andere Harnesses: Nutze nur eine belegte projektlokale Konfiguration.

   Der Schritt ist abgeschlossen, wenn die neue Sitzung genau den beobachteten
   Affinity-Server entdeckt. Benutzer- und Maschinenkonfiguration bleiben
   unverändert.
4. **Observation.** Schreibe den Befund nach
   `schemas/brandkit-affinity-observation.v1.schema.json`, setze eine kurze
   Gültigkeit und binde Inventar sowie Tool-Schemas per SHA-256. Der Schritt ist
   abgeschlossen, wenn das Schema validiert und Status, Lock,
   `mutation_capable` und Blocker logisch übereinstimmen.

## Bridge-Pin

`toolchain/affinity-codex/UPSTREAM.json` ist die einzige Quelle für Repository,
Commit, Lizenz und Byte-Hashes der Codex-Bridge. Prüfe diese Bindung mit dem
lokalen Test, bevor der Launcher als verfügbar gilt. Die Community-Quelle dient
als gepinnter Transportadapter; beobachtetes Serververhalten und Affinitys
Preamble bleiben die Ausführungsautorität.

## Abbruchzustände

Klassifiziere Identitäts-, Berechtigungs- und Inventarfehler als
`unknown|ambiguous`, nicht als Nichtvorhandensein. Ein nicht antwortender,
vollständig geprüfter Kandidat darf `unavailable` ergeben. Eine Observation aus
einem anderen Projekt, Run oder abgelaufenen Zeitfenster ist für den
Vektorworkflow ungültig.
