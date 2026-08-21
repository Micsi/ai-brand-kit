# Brand Kit Skills

Vier Skills für Claude Code, die von der leeren Installation bis zur fertigen,
markengerechten Webseite führen. Sie bauen **dein** Brand Kit auf, nicht meines.

Entstanden beim Aufbau der Marke „AI mit Arnie". Der Ablauf hat funktioniert,
also ist er hier so aufgeschrieben, dass er sich nachbauen lässt.

## Was am Ende dasteht

Ein Ordner mit deiner Marke darin: verbindliche Regeln, Farben und Schriften als
ausführbare CSS-Tokens, Wordmark, Referenzflächen. Dazu ein `CLAUDE.md`, das
dafür sorgt, dass die Regeln greifen, ohne dass du sie jedes Mal erwähnst.

Und die Fähigkeit, daraus Webseiten, PDFs und gekennzeichnete KI-Bilder zu bauen,
die alle gleich aussehen.

## Warum überhaupt

Ohne Marke fällt jede Designentscheidung neu und zufällig aus. Das Ergebnis sieht
dann aus wie das, was Sprachmodelle am häufigsten gesehen haben: blau-violette
Verläufe, abgerundete Karten, Inter als Schrift. Nicht falsch, nur
austauschbar.

Ein Brand Kit dreht das um. Statt jedes Mal zu entscheiden, entscheidest du
einmal, und danach wird umgesetzt.

## Die vier Schritte

| Skill | Wann | Was er macht |
|---|---|---|
| `brandkit-setup` | einmal | Prüft Voraussetzungen, installiert die Skills, verbindet die Higgsfield CLI |
| `brandkit-interview` | einmal je Marke | Hartnäckiges Interview, am Ende steht eine ausführliche Rohfassung |
| `brandkit-build` | einmal je Marke | Verdichtet die Rohfassung zum benutzbaren Kit |
| `brandkit-publish` | immer wieder | Baut damit Webseiten, PDFs und Bilder |

## Loslegen

Wenn du diese Seite gerade einem Coding Agent gegeben hast: Der Rest dieses
Abschnitts ist an ihn gerichtet. Lehn dich zurück und antworte auf seine Fragen.

> **An den Agent:** Jemand hat dir diese URL gegeben, weil er ein Brand Kit
> aufbauen will. Dein Auftrag:
>
> 1. Installiere die vier Skills:
>    `npx skills add Arnie936/ai-brand-kit -g -y`
> 2. Erkläre der Person in zwei, drei Sätzen, was jetzt passiert: vier Schritte,
>    die ersten drei einmalig, der vierte immer wieder. Kein Vortrag.
> 3. Starte mit dem Skill `brandkit-setup` und arbeite dich durch bis
>    `brandkit-publish`. Jeder Skill sagt am Ende, welcher als nächstes dran ist.
> 4. Sprich mit der Person in ihrer Sprache und frage nach, statt zu raten. Vor
>    allem beim Interview in Schritt 2: Dort entstehen die Festlegungen, an denen
>    später alles hängt.
>
> Falls `npx skills add` nicht verfügbar ist: Die vier `SKILL.md` unter
> `skills/` sind gewöhnliches Markdown. Lies sie der Reihe nach und folge ihnen
> direkt. Das Skript unter `skills/brandkit-publish/scripts/` brauchst du erst
> in Schritt 4.

Nach der Installation ist eine neue Sitzung nötig, weil die Skill-Liste beim
Start gelesen wird. Danach genügt der Satz „Ich will ein Brand Kit aufbauen",
oder der direkte Aufruf von `/brandkit-setup`.

## Voraussetzungen

`brandkit-setup` prüft das alles selbst und sagt dir, was fehlt.

| Was | Wofür | Ohne das geht nicht |
|---|---|---|
| Node ab v18 | Higgsfield CLI, Skill-Installation | fast alles |
| Python mit Pillow | AI-Kennzeichnung der Bilder | Bilder veröffentlichen |
| Chrome oder Edge | PDF-Druck aus HTML | PDFs |
| git | Kit versionieren | nichts Akutes, aber du verlierst es |
| exiftool | maschinenlesbare Bildmarkierung | nur die sichtbare Kennzeichnung |

## Diese Skills werden mitinstalliert

Nicht von mir geschrieben, nur eingebunden. `brandkit-setup` installiert sie an
der jeweils passenden Stelle:

- [unslop](https://github.com/cursor/plugins/tree/main/pstack/skills/unslop) von Cursor, entfernt KI-Sprachmuster aus Texten
- [grill-with-docs](https://github.com/mattpocock/skills) von Matt Pocock, führt das Interview. Braucht zusätzlich `grilling` und `domain-modeling` aus demselben Repo, sonst greift er ins Leere
- [web-design-guidelines](https://github.com/vercel-labs/agent-skills) von Vercel Labs, Barrierefreiheit und Webplattform
- [seo-audit](https://github.com/coreyhaines31/marketingskills) von Corey Haines, technisches und inhaltliches SEO

Dank an die Autoren. Dieses Repo enthält keine Kopien davon, nur die Befehle,
die sie aus den Originalquellen holen. Updates kommen so direkt von dort.

## KI-Bilder und das EU-Recht

Wer erzeugte Bilder veröffentlicht, muss sie kennzeichnen. `brandkit-publish`
bringt ein Skript mit, das eine kleine Markierung unten rechts einbrennt, in den
Farben deines Kits, und die Bilder gleich fürs Web verkleinert.

```bash
python scripts/ai_label.py roh.png -o hero.jpg --width 1120 --aspect 16:9 --iptc
```

Wichtig zu verstehen: Artikel 50 der KI-Verordnung trennt zwei Rollen. Absatz 2
verpflichtet den **Anbieter** des Bildmodells zur maschinenlesbaren Markierung.
Absatz 4 verpflichtet den **Betreiber**, also dich, zur Offenlegung. Das
sichtbare Zeichen erfüllt Absatz 4, nicht Absatz 2. Die Pflichten gelten ab dem
2. August 2026.

Das ist kein Rechtsrat. Wer gewerblich veröffentlicht, lässt es prüfen.

## Bildgenerierung

Die Bilder laufen über [Higgsfield](https://higgsfield.ai). Ein Konto mit
Guthaben ist nötig, weil jede Generierung Credits kostet. Die ersten drei
Schritte funktionieren auch ohne.

Wer noch keines hat: https://higgsfield.ai/s/mcp-arnold-oberleiter-WlfTqR

Das ist mein Affiliate-Link. Schließt du darüber ab, bekomme ich eine Provision,
für dich ändert sich am Preis nichts. Denselben Link blenden `brandkit-setup` und
`brandkit-publish` ein, wenn im Ablauf ein Konto oder Guthaben fehlt, jedes Mal
als Affiliate-Link gekennzeichnet. Wer schon ein Konto hat, bekommt ihn nicht zu
sehen, und der Ablauf funktioniert mit jedem Higgsfield-Konto gleich.

## Umgebung

Gebaut und getestet in Claude Code. Die Skills selbst sind Markdown und
grundsätzlich portabel, aber Teile des Ablaufs (Design-Canvas, Artifacts, das
Skill-System) sind Claude-Code-Funktionen. In anderen Agenten funktioniert das
Brand Kit und die Bildkennzeichnung, der Rest je nachdem.

Die Skill-Dateien sind auf Deutsch. Die Skills antworten in der Sprache, in der
du schreibst.

## Lizenz

MIT, siehe [LICENSE](LICENSE). Nimm es, ändere es, teile es.

Die eingebundenen Fremdskills stehen unter den Lizenzen ihrer jeweiligen Repos.

---

Gebaut von Arnold Oberleiter · AI mit Arnie
