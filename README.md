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
 ┌──────────────────────────┐   op koppen knippen, 300-1.800 tekens per stuk,
 │ chunking                 │   frontmatter-omschrijving als context;
 └──────────────────────────┘   JSON: één stuk per item + één overzichtsstuk
        │
        ├──────────────► FTS5-woordindex (BM25, titel ×6, sectie ×3)
        │
        └──────────────► vectorindex (e5-embeddings, lokaal, CPU)
                                 │
 vraag ──► woordlijst ──────┐    │
       ├─► betekenislijst ──┼──► RRF-fusie ──► herrangschikking ──► top-k ──► aanvullen
       └─► lijst per naam ──┘                  (afgerond ×0,5,             (andere stukken
                                                nieuwste iets omhoog)       van een raak
                                                                            document)
```

Alles zit in één SQLite-bestand. Bij elke bouw wordt een nieuw bestand gemaakt en pas
aan het eind atomisch omgewisseld, zodat er nooit een halve index actief is. Een
embedding-cache zorgt dat een herbouw alleen gewijzigde stukken opnieuw berekent.
`polaris ververs` gaat verder: het leest alleen gewijzigde bestanden opnieuw en werkt de
index in één transactie bij, zodat een wijziging binnen een à twee seconden doorzoekbaar
is.

## Snel proberen

```bash
pip install -r requirements.txt
python -m polaris.cli bouw --config voorbeelden/demo/polaris.toml
python -m polaris.cli zoek --config voorbeelden/demo/polaris.toml "access point krijgt geen stroom"
```

De demo laat ook de beveiliging zien: een wachtwoord in `netwerk-basis.md` wordt
gemaskeerd, `salarisoverzicht.md` wordt overgeslagen via een opt-out, en een taak met
`vertrouwelijk: true` komt niet in de index.

En hij laat zien wat een document met een tegenspraak oplevert. In
`verlof-karin-najaar.md` zegt de samenvatting dat Ruben Bakkerij Vermeulen houdt, en een
tabel verderop zegt Yusuf:

```bash
python -m polaris.cli zoek --config voorbeelden/demo/polaris.toml -k 1 \
    "wie is de waarnemer voor Bakkerij Vermeulen tijdens het verlof van Karin"
```

De treffer is de tabel, en onder "ook in dit document" staat de samenvatting. Wie de
treffers leest (een mens of een assistent) ziet zo beide en kan de tegenspraak noemen.

## Installeren

Vereist Python 3.10 of nieuwer.

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
  (`##` t/m `####`). Frontmatter wordt gebruikt: `description` gaat als context vóór
  elk stuk, `status`, `title` en `date` worden overgenomen, `polaris: nee` slaat het
  bestand over.
- **`json_lijst`** — een JSON-bestand met een array van items (taken, kaarten, tickets).
  Veldnamen zijn instelbaar (`id_veld`, `titel_veld`, `tekst_veld`, `status_veld`,
  `datum_veld`, `sectie_veld`), zodat het bij elk systeem past. Met `alle_velden = true`
  gaat elk veld mee als `sleutel: waarde`. Per lijst komt er één overzichtsstuk bij met
  alle namen (`overzicht = false` zet dat uit).

Andere bronnen (commit-berichten, een live inventaris, een database) lever je vanuit je
eigen programma aan met `extra_stukken` — zie "Vanuit een ander programma".

Zoekgedrag in `[algemeen]`:

| Instelling | Standaard | Wat het doet |
|---|---|---|
| `actualiteit_bonus`, `actualiteit_dagen` | 0,25 en 730 | hoeveel een recent stuk voorgaat op een even goed ouder stuk; 0 = uit |
| `stemmer` | `"grof"` | `"nl"` = Nederlandse Snowball-stemmer (`pip install snowballstemmer`, index herbouwen), `"uit"` = alleen exacte woorden |
| `snippets`, `snippet_markering` | uit, `["[", "]"]` | per treffer een fragment rond de gevonden woorden |
| `threads` | 0 = alle kernen | CPU-threads voor het model |
| `namen_gewicht` | 0,7 | gewicht van de extra woordlijst per eigennaam uit de vraag; 0 = uit |
| `aanvullen` | 2 | hoeveel andere stukken een raak document hoogstens meelevert; 0 = uit |

**Namen.** Een eigennaam of afkorting in de vraag (een woord met een hoofdletter dat niet
vooraan een zin staat, of een woord in hoofdletters) krijgt een eigen woordzoektocht in de
fusie, en noemt de vraag er meer, dan krijgen de stukken met álle namen er nog een lijst
bij. Zonder dit kan een stuk dat de klant uit de vraag terloops noemt, wegzakken achter
stukken die alleen over hetzelfde onderwerp gaan. Een vraag in kleine letters heeft geen
namen; dan verandert er niets.

**Aanvullen.** Lange documenten worden in stukken geknipt. Is een document raak, dan
komen de andere stukken ervan die de namen (of, zonder namen, de woorden) uit de vraag
bevatten mee in het veld `ook_in_dit_document`, in documentvolgorde. Een samenvatting en
een tabel verderop die elkaar tegenspreken, staan zo naast elkaar in plaats van dat er
één wordt gekozen. Staat er een `description` in de frontmatter, dan staat die vóór elk
stuk; noemt die een naam uit de vraag, dan telt elk stuk van dat document als raak en
bepaalt BM25 welke er meekomen.

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
| `polaris ververs --config polaris.toml` | Alleen bijwerken wat veranderd is; wordt vanzelf een volledige bouw als dat nodig is |
| `polaris volg --config polaris.toml` | Blijven draaien en verversen zodra een wijziging 20 s stil is (`--rust`, `--interval`) |
| `polaris zoek --config polaris.toml "vraag"` | Zoeken; `-k 5` voor minder resultaten, `--bron taken` voor één bron, `--json` voor machine-uitvoer |
| `polaris info --config polaris.toml` | Model, bouwmoment, aantal stukken per bron, versie en schema van de index |
| `polaris eval --config polaris.toml vragen.json` | Meetlat: staat het verwachte resultaat in de top-k, en komen alle verwachte stukken mee? Geeft plek per vraag, MRR en compleetheid; `-k`, `--json` |
| `polaris model --config polaris.toml` | Model vooraf ophalen (voor offline gebruik) |
| `polaris versie` | Versienummer van Polaris |
| `polaris bijwerken` | Nieuwste versie ophalen via git |

Zonder `pip install -e .` gebruik je `python -m polaris.cli` in plaats van `polaris`.

### Vanuit een ander programma

```bash
polaris zoek --config polaris.toml --json -k 5 "vraag"
```

geeft een JSON-lijst met per treffer `id`, `bron`, `pad`, `titel`, `sectie`, `datum`,
`status`, `tekst`, `verwijzingen`, `volgnr` (plek in het document) en `score`, en waar
van toepassing `ook_in_dit_document`: een lijst met `id`, `sectie`, `volgnr` en `tekst`
van de andere stukken van dat document die erbij horen. Elke CLI-aanroep laadt wel het model
opnieuw (1-3 s). In een langlopend proces gebruik je daarom de `Zoeker`, die model en
vectormatrix warm houdt:

```python
from polaris import config, index
cfg = config.laad("polaris.toml")
zoeker = index.Zoeker(cfg)                 # één keer aanmaken, daarna hergebruiken
for treffer in zoeker.zoek("access point krijgt geen stroom", k=5, bron=None):
    print(treffer["titel"], treffer["score"])
```

`index.zoek(cfg, vraag)` doet hetzelfde en houdt zelf één Zoeker per index in leven.
De matrix wordt automatisch opnieuw ingelezen zodra het indexbestand door een bouw is
vervangen; een webserver hoeft dus niet te herstarten na een herbouw.

**Eigen bronnen aanleveren.** Heeft je programma kennis die niet in markdown of een
JSON-lijst zit (commit-berichten, een live inventaris, rijen uit een database), geef die
dan als stukken mee aan de bouw. `bron`, `pad` en `tekst` zijn verplicht; `titel`,
`sectie`, `datum`, `status` en `id` zijn optioneel. Ze krijgen dezelfde maskering,
ontdubbeling en embedding als de eigen bronnen:

```python
extra = [{"bron": "git", "pad": "commit 1a2b3c", "titel": "Firewall aangescherpt",
          "datum": "2026-09-01", "tekst": "Poort 22 alleen nog vanaf het beheernetwerk."}]
index.bouw(cfg, extra_stukken=extra)
```

Stukken met dezelfde `bron` en hetzelfde `pad` vormen samen één document; hun volgorde
in de lijst is de documentvolgorde (of geef zelf `volgnr` mee).

**Na een wijziging bijwerken.** Wanneer er gebouwd moet worden, weet je programma zelf het
best. Roep dan `index.ververs(cfg, extra_stukken=extra)` aan met de volledige, actuele
lijst: Polaris vergelijkt per stuk en schrijft alleen wat nieuw, gewijzigd of verdwenen
is. Het resultaat is precies dezelfde index als een volledige bouw. Kan verversen niet
(nog geen index, een oudere indexstructuur, een andere config of Polaris-versie), dan
wordt het vanzelf een volledige bouw; de teruggegeven dict zegt dat met `volledig`.

Voor tests of een andere embedder: `bouw(cfg, embedder=...)` en `Zoeker(cfg, embedder=...)`
accepteren elk object met `passages(teksten)` en `query(tekst)` die genormaliseerde
vectoren teruggeven. De tests gebruiken zo een nep-embedder zonder model.

Het veld `verwijzingen` bevat ID's of ticketnummers die in de tekst genoemd worden
(patronen instelbaar via `verwijzingspatronen`). Het is een signaal voor het aanroepende
programma: dit stuk gaat over iets in een extern systeem, en voor de actuele stand is
het beter daar vers te kijken dan de index te vertrouwen.

## Meten: de evaluatieset

Elke wijziging aan chunking, gewichten, stemmer of model is pas een verbetering als de
cijfers dat zeggen. Maak daarom een lijst vragen met wat er in de top-k moet staan:

```json
[
  {"vraag": "access point krijgt geen stroom", "verwacht": "taken\\.json#TK-1"},
  {"vraag": "hoe zet ik een back-up terug",   "verwacht": "back-up-procedure", "bron": "documenten"},
  {"vraag": "wie is de waarnemer voor Bakkerij Vermeulen", "verwacht": "verlof-karin", "k": 1,
   "verwacht_alle": ["^Samenvatting$", "^Waarneming per klant$"]}
]
```

`verwacht` is een reguliere expressie op het pad of de titel van een treffer; `k` per
vraag overschrijft de k van de run. `verwacht_alle` is een tweede controle: elke
expressie moet passen op het pad, de titel of de sectie van een treffer of van een stuk
in `ook_in_dit_document`. Zo meet je of een raak document al zijn relevante stukken
levert. Dan:

```
polaris eval --config polaris.toml vragen.json
 1  access point krijgt geen stroom
 2  hoe zet ik een back-up terug
 1  wie is de waarnemer voor Bakkerij Vermeulen
gevonden: 3/3   MRR 0.833   compleet: 1/1   (top-8, 0.3 s)
```

MRR is het gemiddelde van 1/plek (0 als niet gevonden): 1,0 betekent alles bovenaan.
Draai dit vóór en na elke wijziging. Tien tot twintig vragen die je zelf ooit hebt
gesteld zijn genoeg om een verslechtering te zien; neem er een paar bij waarin het
kernwoord níet letterlijk in de bron staat, want daar zit het verschil tussen woorden
en betekenis. Zie `voorbeelden/demo/eval.json`.

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
Gebruik je `polaris ververs`, dan gebeurt dat laatste vanzelf: de eerste ververs na een
nieuwe versie is een volledige bouw.

## Richting voor volgende versies

- **Warme zoekservice.** De `Zoeker` houdt sinds 0.2.0 model en matrix warm binnen één
  proces; wat ontbreekt is een klein, altijd-aan proces met een netwerk-endpoint, zodat
  ook de CLI en andere machines daarvan profiteren (zoekactie ~0,1 s). Zo'n service
  moet authenticatie krijgen; zie `docs/beveiliging.md`. `polaris volg` zou er de
  bijwerk-kant van kunnen worden.
- **Database als brontype.** Rechtstreeks uit een database lezen in plaats van uit een
  export, met een expliciete lijst van tabellen en kolommen die mogen — nooit "alles".
- **Leerlus.** Zoekopdrachten zonder goede treffer loggen en periodiek gericht
  trefwoorden aan de gemiste stukken toevoegen (document-expansie).
- **Reranker.** Een tweede, zwaarder model dat alleen de top-30 herbeoordeelt door vraag
  en stuk sámen te lezen; als optie, want het kost op een kleine CPU 0,3 tot 1 s per
  vraag. Alleen als de evaluatieset laat zien dat de fusie tekortschiet.

Bewust **niet** op de lijst: een vectordatabase, een groter embeddingmodel, alles in
één prompt, of een taalmodel dat trefwoorden bij stukken verzint. Bij duizenden tot
tienduizenden stukken op gewone hardware leveren die complexiteit zonder meetbare
winst, en het laatste haalt een LLM in een pijplijn die daar juist vrij van is.

## Bestanden

| Bestand | Inhoud |
|---|---|
| `polaris/index.py` | Chunking, frontmatter, embedder, bouwen (met lock en cache), `Zoeker` |
| `polaris/beveiliging.py` | Selectie, opt-out, maskeren |
| `polaris/config.py` | Inlezen van `polaris.toml` |
| `polaris/eval.py` | Evaluatieset draaien, MRR |
| `polaris/cli.py` | Opdrachtregel |
| `polaris/update.py` | Bijwerken via git |
| `polaris/volg.py` | Volg-modus: verversen bij elke wijziging |
| `tests/` | Tests zonder model of netwerk |
| `voorbeelden/polaris.toml` | Configuratiesjabloon met alle opties |
| `voorbeelden/demo/` | Kleine kennisbank om mee te proberen, met `eval.json` |
| `docs/techniek.md` | Onderbouwing van elke technische keuze |
| `docs/beveiliging.md` | Waar indexen horen, risico's, aanbevelingen |
| `pakket.py` | Zip bouwen uit een release-tag |
| `LICENSE`, `NOTICE` | Apache License 2.0 |

## Licentie

Polaris valt onder de [Apache License 2.0](LICENSE). Je mag het gebruiken, aanpassen en
verspreiden, ook commercieel, zolang je de licentie en `NOTICE` meelevert en aangeeft
welke bestanden je hebt gewijzigd. De gebruikte embedding-modellen hebben hun eigen
licentie (MIT, zie [Installeren](#installeren)).
