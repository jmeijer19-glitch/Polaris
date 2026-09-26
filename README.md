# Polaris

**Hybride zoekindex over je eigen kennisbank, volledig lokaal.**

Polaris zoekt tegelijk op *woorden* (wat er letterlijk staat) en op *betekenis* (wat er
ongeveer bedoeld wordt), en voegt die twee samen tot één ranglijst. Vraag je "access
point krijgt geen stroom", dan vindt het ook het document over "PoE-budget op de
switch" — zonder dat die woorden overeenkomen.

Polaris is **puur de zoektechniek**: het bouwt een index en geeft de best passende
stukken tekst terug. Er zit geen taalmodel (LLM) in dat antwoorden formuleert, en er gaat
geen tekst naar een externe dienst — het embeddingmodel draait lokaal op de CPU. Wat je
met de resultaten doet (tonen, doorgeven aan een assistent, in een app zetten) is aan
het programma dat Polaris aanroept.

---

## Waarom hybride

| Aanpak | Sterk in | Zwak in |
|---|---|---|
| Alleen woorden (klassiek zoeken) | Exacte termen, namen, nummers, codes | Synoniemen, andere formulering, vervoegingen |
| Alleen betekenis (embeddings) | Synoniemen, omschrijvingen, "ongeveer dit" | Exacte codes en zeldzame namen; soms vage treffers |
| **Beide, samengevoegd** | **Allebei** | Iets meer rekenwerk bij het bouwen |

Polaris combineert ze met *Reciprocal Rank Fusion* (RRF): elk stuk krijgt punten op basis
van zijn plek in elke lijst, niet op basis van ruwe scores die onderling niet
vergelijkbaar zijn. Zie [`docs/techniek.md`](docs/techniek.md) voor de volledige
onderbouwing van elke keuze.

## Hoe het werkt

```
 bronnen (markdown, JSON)
        │
        ▼
 ┌──────────────────────────┐   selectie per bron, opt-out,
 │ verzamelen + beveiligen  │   geheimen maskeren
 └──────────────────────────┘
        │
        ▼
 ┌──────────────────────────┐   op koppen knippen, 300-1.800 tekens
 │ chunking                 │   per stuk, JSON: één stuk per item
 └──────────────────────────┘
        │
        ├──────────────► FTS5-woordindex (BM25, titel ×6, sectie ×3)
        │
        └──────────────► vectorindex (e5-embeddings, lokaal, CPU)
                                 │
 vraag ──► woordlijst ──┐        │
       └─► betekenislijst ┴──► RRF-fusie ──► herrangschikking ──► top-k
                                              (afgerond ×0,5)
```

Alles zit in één SQLite-bestand. Bij elke bouw wordt een nieuw bestand gemaakt en pas
aan het eind atomisch omgewisseld, zodat er nooit een halve index actief is. Een
embedding-cache zorgt dat een herbouw alleen gewijzigde stukken opnieuw berekent.

## Snel proberen

```bash
pip install -r requirements.txt
python -m polaris.cli bouw --config voorbeelden/demo/polaris.toml
python -m polaris.cli zoek --config voorbeelden/demo/polaris.toml "access point krijgt geen stroom"
```

De demo laat ook de beveiliging zien: een wachtwoord in `netwerk-basis.md` wordt
gemaskeerd, `salarisoverzicht.md` wordt overgeslagen via een opt-out, en een taak met
`vertrouwelijk: true` komt niet in de index.

## Installeren

Vereist Python 3.11 of nieuwer.

```bash
pip install -r requirements.txt   # alleen de afhankelijkheden
pip install -e .                  # of: installeren, dan is `polaris` overal een commando
```

### Het lokale model

Het embeddingmodel zit **niet** in deze repository (e5-base is ~280 MB; te groot voor
git en niet nodig om mee te versioneren). Er zijn twee manieren om het beschikbaar te
maken:

1. **Automatisch** — bij de eerste `polaris bouw` (of expliciet met `polaris model`)
   wordt het eenmalig van Hugging Face gedownload naar `~/.cache/polaris-modellen`.
   Daarna is er geen internet meer nodig.
2. **Offline** — voor een machine zonder internet: haal het model op een andere machine
   op met `polaris model`, kopieer de getoonde map, en zet in de config
   `model_pad = "<pad naar de kopie>"`. Polaris downloadt dan nooit iets.

| Model | Grootte | Dimensies | Wanneer |
|---|---|---|---|
| `intfloat/multilingual-e5-base` (standaard) | ~280 MB | 768 | Normale werkplek of server |
| `intfloat/multilingual-e5-small` | ~120 MB | 384 | Zwakke hardware, demo's |

Beide modellen zijn meertalig (inclusief Nederlands) en vallen onder de MIT-licentie.
Wissel je van model, bouw de index dan opnieuw — Polaris weigert te zoeken in een index
die met een ander model is gebouwd dan de config noemt.

## Configureren

Eén `polaris.toml` per installatie. Kopieer [`voorbeelden/polaris.toml`](voorbeelden/polaris.toml)
en pas de bronnen aan. Relatieve paden gelden ten opzichte van het configbestand.

Twee brontypen:

- **`markdown`** — een map met `.md`-bestanden, recursief, geknipt op koppen
  (`##` t/m `####`).
- **`json_lijst`** — een JSON-bestand met een array van items (taken, kaarten, tickets).
  Veldnamen zijn instelbaar (`id_veld`, `titel_veld`, `tekst_veld`, `status_veld`,
  `datum_veld`, `sectie_veld`), zodat het bij elk systeem past.

## Beveiliging

> **Een zoekindex is een kopie van alles wat erin gaat.** Wie het indexbestand kan
> lezen, kan alles lezen wat geïndexeerd is — de toegangsrechten van de oorspronkelijke
> bestanden gaan niet mee.

Daarom beslist Polaris al bij het bouwen wat erin mag:

| Laag | Hoe | Voorbeeld |
|---|---|---|
| Selectie per bron | `alleen` / `uitsluiten` met glob-patronen | `uitsluiten = ["hr/**", "*contract*"]` |
| Opt-out per document | `polaris: nee` in de frontmatter van een markdown-bestand | zie `voorbeelden/demo` |
| Opt-out per item | `uitsluit_veld` voor JSON-items | `uitsluit_veld = "vertrouwelijk"` |
| Maskeren | wachtwoorden, tokens, sleutels → `[verborgen]` | staat standaard aan |

Na elke bouw meldt Polaris hoeveel bestanden zijn uitgesloten, hoeveel opt-outs er
waren en hoeveel geheimen gemaskeerd. Lees [`docs/beveiliging.md`](docs/beveiliging.md)
vóór je een index bouwt over gevoelige informatie: daar staat waar je indexen
aanmaakt, hoe je met verschillende doelgroepen omgaat, en wat maskeren wél en níet
afvangt.

## Opdrachten

| Opdracht | Wat het doet |
|---|---|
| `polaris bouw --config polaris.toml` | Index (her)bouwen |
| `polaris zoek --config polaris.toml "vraag"` | Zoeken; `-k 5` voor minder resultaten, `--json` voor machine-uitvoer |
| `polaris info --config polaris.toml` | Model, bouwmoment, aantal stukken, versie van de index |
| `polaris model --config polaris.toml` | Model vooraf ophalen (voor offline gebruik) |
| `polaris versie` | Versienummer van Polaris |
| `polaris bijwerken` | Nieuwste versie ophalen via git |

Zonder `pip install -e .` gebruik je `python -m polaris.cli` in plaats van `polaris`.

### Vanuit een ander programma

```bash
polaris zoek --config polaris.toml --json -k 5 "vraag"
```

geeft een JSON-lijst met per treffer `bron`, `pad`, `titel`, `sectie`, `datum`,
`status`, `tekst`, `verwijzingen` en `score`. Of rechtstreeks in Python:

```python
from polaris import config, index
cfg = config.laad("polaris.toml")
for treffer in index.zoek(cfg, "access point krijgt geen stroom", k=5):
    print(treffer["titel"], treffer["score"])
```

Het veld `verwijzingen` bevat ID's of ticketnummers die in de tekst genoemd worden
(patronen instelbaar via `verwijzingspatronen`). Het is een signaal voor het aanroepende
programma: dit stuk gaat over iets in een extern systeem, en voor de actuele stand is
het beter daar vers te kijken dan de index te vertrouwen.

## Versies en bijwerken

Deze repository is de centrale versie; elke installatie is een `git clone` die zich
bijwerkt met `polaris bijwerken` (een `git pull --tags`). Elke release heeft een tag
(`v0.1.0`) en een regel in [`CHANGELOG.md`](CHANGELOG.md).

**Een nieuwe versie uitbrengen:**

1. Wijzig de code en draai de tests: `python -m unittest discover -s tests`.
2. Voeg een sectie toe aan `CHANGELOG.md` en hoog het versienummer op in `VERSION` én
   `pyproject.toml`.
3. Commit, tag en push:
   ```bash
   git commit -am "Polaris 0.2.0: <wat er beter is>"
   git tag v0.2.0
   git push && git push --tags
   ```
4. Optioneel: `python pakket.py` maakt `dist/polaris-<versie>.zip` uit de tag, voor wie
   geen git-toegang heeft.

Na een update: `pip install -r requirements.txt` als er afhankelijkheden bij zijn
gekomen, en `polaris bouw` als de CHANGELOG zegt dat de indexstructuur is veranderd.

## Richting voor volgende versies

- **Warme zoekservice.** Nu laadt elke `polaris zoek` het model opnieuw (2-3 seconden).
  Een klein, altijd-aan proces dat het model in het geheugen houdt, maakt een zoekactie
  ~0,1 seconde — nodig voor snel zoeken vanaf een telefoon of door meerdere gebruikers.
  Zo'n service moet authenticatie krijgen; zie `docs/beveiliging.md`.
- **Database als brontype.** Rechtstreeks uit een database lezen in plaats van uit een
  export, met een expliciete lijst van tabellen en kolommen die mogen — nooit "alles".
- **Leerlus.** Zoekopdrachten zonder goede treffer loggen en periodiek gericht
  trefwoorden aan de gemiste stukken toevoegen (document-expansie).
- **Evaluatieset.** Een vaste set vragen met verwacht resultaat, zodat elke wijziging
  meetbaar beter of slechter is.

## Bestanden

| Bestand | Inhoud |
|---|---|
| `polaris/index.py` | Chunking, embedder, bouwen, zoeken |
| `polaris/beveiliging.py` | Selectie, opt-out, maskeren |
| `polaris/config.py` | Inlezen van `polaris.toml` |
| `polaris/cli.py` | Opdrachtregel |
| `polaris/update.py` | Bijwerken via git |
| `tests/` | Tests zonder model of netwerk |
| `voorbeelden/polaris.toml` | Configuratiesjabloon met alle opties |
| `voorbeelden/demo/` | Kleine kennisbank om mee te proberen |
| `docs/techniek.md` | Onderbouwing van elke technische keuze |
| `docs/beveiliging.md` | Waar indexen horen, risico's, aanbevelingen |
| `pakket.py` | Zip bouwen uit een release-tag |
