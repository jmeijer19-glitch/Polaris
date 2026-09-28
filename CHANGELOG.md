# Changelog — Polaris

Alle betekenisvolle wijzigingen per versie, nieuwste bovenaan. Versienummers volgen
[semantic versioning](https://semver.org/lang/nl/): een verhoging van het middelste
getal betekent nieuwe functionaliteit, van het laatste getal een reparatie. Staat er
**index opnieuw bouwen** bij een versie, draai dan na het bijwerken `polaris bouw`.

## 0.5.0 — 2026-09-28

Aanleiding: een programma zocht naast zijn Polaris-index ook in een documentarchief en
plakte die treffers achter de eigen lijst. Afgeknipt op de top-8 kwam er dus nooit een
document mee, en een vraag waarvan het antwoord op een factuur stond, werd verkeerd
beantwoord. Documenten gewoon op score ertussen zetten ging ook mis: een paar papieren die
op één toevallig woord raakten, duwden de juiste stukken uit de top.

- **`index.samenvoegen(hoofd, extra, max_per_bron=2, min_signalen=2)`**: treffers uit
  meerdere indexen tot één lijst. Per extra index dringen hoogstens `max_per_bron` treffers
  op score naar voren, alleen als minstens `min_signalen` lijsten ze vonden; de rest komt
  achteraan. Reken bij het afknippen die plekken erbij, dan valt er geen eigen treffer weg.
- **`index.rrf(lijsten)`**: de reciprocal rank fusion van de `Zoeker` als losse functie,
  voor wie een eigen zoeker heeft. Zelfde scoreschaal (plek 1 = 1/(k+1)), zodat scores uit
  verschillende indexen naast elkaar te leggen zijn. Twee eigen kopieën van deze fusie
  (met plek 0 = 1/k, dus net een andere schaal) zijn daarmee overbodig geworden.
- Elke treffer van de `Zoeker` heeft nu het veld **`signalen`**: welke lijsten het stuk
  vonden (`woorden`, `betekenis`, `namen`). Rangorde en scores zijn ongewijzigd.
- Gemeten op een kennisbank van 4.415 stukken plus een archief van ~3.000 documenten, 22
  vragen: 19/20 → 21/22 in de voorzoek, geen vraag gezakt.

Geen nieuwe indexstructuur. Wie `polaris ververs` gebruikt, krijgt door de nieuwe versie
eenmalig een volledige bouw.

## 0.4.1 — 2026-09-28

- Alleen documentatie, een commentaar en een test: voorbeelden uit een echte kennisbank
  vervangen door verzonnen namen. Geen functionele wijziging; wie `polaris ververs`
  gebruikt, krijgt door de nieuwe versie eenmalig een volledige bouw.

## 0.4.0 — 2026-09-28 — **index opnieuw bouwen** (of `polaris ververs`, dat doet het vanzelf)

Aanleiding: in een kennisbank van ~1.100 stukken gaf een vraag een verkeerd antwoord
terwijl het goede antwoord wél in de index stond. Een document had een samenvatting
("persoon A blijft bij klant X") en verderop een tabel ("klant X → persoon B"); de vraag
raakte alleen de samenvatting. En een stuk dat de klant terloops noemde, stond met de hele
vraag op plek 12, met alleen de klantnaam op plek 1.

**Aanvullen per document**
- Is een document raak, dan komen tot `aanvullen` (standaard 2) andere stukken van dat
  document mee die de namen of woorden uit de vraag bevatten: veld `ook_in_dit_document`
  bij de hoogste treffer, in documentvolgorde. Een samenvatting en een tabel die haar
  tegenspreekt, staan zo naast elkaar. Rangorde en MRR veranderen er niet door.
- Elke treffer heeft nu `volgnr`, de plek in het document. Aangeleverde stukken met
  dezelfde bron en hetzelfde pad vormen één document, in de volgorde van aanleveren.

**Namen wegen mee**
- Eigennamen en afkortingen uit de vraag krijgen elk een eigen woordlijst in de fusie
  (`namen_gewicht`, standaard 0,7), plus bij twee of meer namen een lijst met de stukken
  die ze allemaal noemen. Een vraag in kleine letters zoekt precies als voorheen.
- Gemeten op een kennisbank van 4.414 stukken, 20 vragen: MRR 0,67 → 0,71, geen vraag
  gezakt. Gewicht 1,5 gaf 0,61 en een vraag minder; vandaar 0,7.

**Verversen in plaats van herbouwen**
- **`polaris ververs`** / `index.ververs(cfg, extra_stukken=...)`: alleen nieuwe,
  gewijzigde en verdwenen stukken, in één transactie. Markdown per bestand (wijzigingstijd
  en grootte), JSON-lijsten per item, aangeleverde stukken per stuk. Het resultaat is
  gelijk aan een volledige bouw (getest, ook bij ontdubbelde tekst). Kan het niet (geen
  index, oudere structuur, andere config of Polaris-versie), dan wordt het vanzelf een
  volledige bouw.
- Gemeten op 4.414 stukken: volledige bouw 3,1 s, verversen zonder wijzigingen 0,6 s, met
  één gewijzigd stuk 1,6 s (waarvan ~0,9 s model laden).
- **`polaris volg`**: blijft draaien en ververst als een wijziging aan de bronbestanden
  20 s stil is (`--rust`, `--interval`).

**Meetlat**
- `verwacht_alle` in de evaluatieset: staan álle genoemde stukken (pad, titel of sectie) in
  de uitkomst, inclusief `ook_in_dit_document`? Het rapport toont "compleet: x/y".
- `k` per vraag.
- Het geval hierboven zit met verzonnen namen in de demo (`verlof-karin-najaar.md`,
  `storing-kassasysteem.md`) en in de tests.
- Demo-evaluatie: 7/7, MRR 1,000, compleet 1/1. Zonder aanvullen: compleet 0/1.

**Overig**
- Indexschema 4: kolommen `volgnr`, `bestand`, `inhoud` en `ruw` in de stuktabel, tabel
  `bronbestand`, en de config-vingerafdruk in `meta`.
- 57 tests (was 44).

## 0.3.1 — 2026-09-26

- Een config zonder `[[bron]]` is toegestaan: een aanroepend programma dat álle stukken
  zelf aanlevert (`extra_stukken`) hoeft geen lege map meer op te geven. De bouw weigert
  alleen als er dan ook niets is aangeleverd.

## 0.3.0 — 2026-09-26 — **index opnieuw bouwen**

Deze versie gaat over meten en over twee opties die je aanzet als je ze nodig hebt.
Alles wat nieuw is heeft een standaard die niets extra's vraagt.

**Meetlat**
- **`polaris eval vragen.json`**: een evaluatieset van vragen met een verwacht resultaat
  (reguliere expressie op pad of titel, optioneel per bron). Uitkomst per vraag de plek
  van de eerste goede treffer, en over de set het aantal gevonden plus de **MRR** (Mean
  Reciprocal Rank). Zonder deze maat is elke wijziging aan chunking, gewichten of model
  een gok. De code zit in de repo; de vragen horen bij de kennisbank waar ze over gaan.
  `voorbeelden/demo/eval.json` laat het formaat zien en haalt 5/5, MRR 1,0.

**Opties**
- **`stemmer = "nl"`**: Nederlandse Snowball-stemmer in plaats van de grove afkap-stam.
  Snowball-stammen zijn geen voorvoegsels van het woord ("betalingen" en "betaald" worden
  allebei "betaal", "storing" wordt "stoor"), dus een wildcard werkt daar niet. Daarom
  krijgt de index een vijfde FTS-kolom met de stammen van titel, sectie en tekst, en
  wordt de vraag op dezelfde manier gestemd: stam tegen stam, exact. Vraagt
  `pip install snowballstemmer` (puur Python, klein) en een herbouw; de index onthoudt
  met welke stemmer hij gebouwd is en weigert een andere config. Standaard blijft
  `"grof"`; `"uit"` doet alleen de exacte poging.
- **`snippets = true`**: per treffer een veld `fragment` met de zin rond de gevonden
  woorden, gemarkeerd met `snippet_markering` (standaard `[` en `]`). SQLite maakt ze,
  het kost niets. Een treffer die alleen op betekenis is gevonden, of via de stam-kolom,
  heeft geen gemarkeerd woord en krijgt het begin van de tekst.

**Robuuster bouwen**
- Tekst en woordindex worden **vóór** het embedden weggeschreven. Een datafout (dubbele
  ID, verkeerd type) komt zo binnen een seconde boven, niet na een kwartier rekenen.
  Gevonden toen een bouw van vierduizend aangeleverde stukken na twaalf minuten strandde.
- Dubbele stuk-ID's (twee bronnen die hetzelfde ID afleiden, of een aanroeper die ze zelf
  toekent) krijgen een afgeleide ID in plaats van de bouw te laten stranden.
- Voortgang tijdens het embedden bij 500 stukken of meer.

**Overig**
- Indexschema 3 (extra FTS-kolom, ook leeg als de stemmer uitstaat).

## 0.2.0 — 2026-09-26 — **index opnieuw bouwen**

Wat er beter is, en waarom. De meeste punten komen uit de praktijk van een grotere
kennisbank (ruim vierduizend stukken, negen bronsoorten) waar dezelfde techniek al een
tijd draait en met een evaluatieset is bijgesteld.

**Zoeken**
- **Warme `Zoeker`.** Model en vectormatrix blijven in het geheugen; de matrix wordt
  alleen opnieuw ingelezen als het indexbestand verandert. `index.zoek()` hergebruikt
  automatisch één Zoeker per index. Een zoekactie in een langlopend proces kost nu
  milliseconden in plaats van het opnieuw laden van het model (was 1,5-3 s per vraag,
  óók vanuit Python).
- **Titel en kopjespad gaan mee in de embedding.** Een sectie "Herstel" zonder de titel
  "Back-upprocedure" erbij wist niet waar hij over ging; nu wel.
- **Actualiteitsbonus** (`actualiteit_bonus`, standaard 0,25): bij twee even goede
  treffers wint de nieuwste; lineair aflopend naar 0 na `actualiteit_dagen` (730).
- **Woordzoeken vult aan in plaats van te stoppen.** Vond de exacte poging maar twee
  stukken, dan worden de stam-pogingen erachter geplakt met lager gewicht. Eerder
  telde alleen de eerste poging die iets vond.
- **Bronfilter**: `zoek(..., bron="taken")` en `polaris zoek --bron taken`.
- **FTS5-syntax gaat ongewijzigd door.** Wie `"…"`, `*` of `OR` typt weet wat hij
  zoekt: alleen de woordindex, geen betekenislijst ertussen.
- `prefix='3 5'` op de FTS-tabel: stam-zoeken met wildcard is nu een indexlookup.
- Status `vervangen` en `deprecated` tellen ook als afgerond.

**Bronnen**
- **Frontmatter** in markdown: `description`/`omschrijving` gaat vóór elk stuk van dat
  document (context voor losse secties), `status` wordt de status van alle stukken,
  `titel`/`title`/`name` overschrijft de bestandsnaam, `datum`/`date` de datum. De
  frontmatter zelf wordt niet meer als tekst geïndexeerd.
- **Overzichtsstuk per JSON-lijst** (`overzicht`, standaard aan): alle namen bij elkaar,
  met sectie en status. "Welke taken zijn er over back-ups" was eerder kansloos omdat het
  antwoord verspreid lag over tientallen losse stukken.
- **`alle_velden = true`** voor JSON-lijsten: elk veld als `sleutel: waarde`-regel in
  plaats van alleen `tekst_veld`.
- **Eigen stukken meegeven**: `index.bouw(cfg, extra_stukken=[...])` voor bronnen die
  Polaris zelf niet kent (commit-berichten, een live inventaris, een database).
  Zelfde maskering en ontdubbeling als de eigen bronnen.
- `stukken_uit_markdown()` is publiek, voor markdown die niet uit een bestand komt.

**Beveiliging**
- Maskering ziet door markdown-opmaak heen: `**Wachtwoord:** \`abc\`` werd eerder
  gemist omdat de sterretjes als waarde golden.
- App-wachtwoorden in groepen (`Abcd1-Efgh2-Ijkl3-Mnop4-Qrst5`) worden gemaskeerd; die
  waren korter dan de drempel van 32 tekens. UUID's en datums blijven staan.

**Bouwen en beheer**
- **Lock** rond een bouw: een tweede bouw (cron naast handmatig) krijgt `BouwBezig` in
  plaats van het tmp-bestand van de eerste te wissen. Een lock ouder dan een uur wordt
  als achtergebleven beschouwd.
- De index onthoudt zijn **schemaversie**; een index van 0.1.0 wordt geweigerd met
  "draai polaris bouw opnieuw" in plaats van stil verkeerde resultaten te geven.
- `threads` in de config (0 = alle kernen). Het aantal staat nu altijd expliciet, wat
  ook de foutmelding over cpu-affinity in containers wegneemt.
- `polaris info` toont het aantal stukken per bron.
- **Python 3.10** wordt weer ondersteund (`tomli` als terugval voor `tomllib`).

**Tests**
- 37 tests (was 17), waaronder een end-to-end reeks bouwen → zoeken → filteren →
  herrangschikken met een deterministische nep-embedder, dus nog steeds zonder model of
  netwerk. `bouw()` en `Zoeker` accepteren elke embedder met `passages()`/`query()`.

## 0.1.0 — 2026-09-26

Eerste release.

**Zoeken**
- Hybride zoekindex: FTS5-woordindex (BM25, titel ×6, sectie ×3) plus embeddings
  (multilingual-e5, int8-ONNX, lokaal op de CPU), samengevoegd met Reciprocal Rank
  Fusion (k = 60).
- Woordzoeken in drie stappen — exact, alle stammen, een van de stammen — met aflopend
  gewicht.
- Afgeronde stukken (status klaar/afgerond/gesloten/…) wegen ×0,5.
- Live-referenties: elk resultaat noemt de ID's die erin voorkomen, met instelbare
  patronen (`verwijzingspatronen`).

**Bronnen**
- `markdown`: mappen met `.md`-bestanden, geknipt op koppen (300-1.800 tekens per stuk).
- `json_lijst`: JSON-array met instelbare veldnamen.
- Relatieve paden gelden ten opzichte van het configbestand.

**Beveiliging**
- Selectie per bron met `alleen` / `uitsluiten`.
- Opt-out per document (`polaris: nee` in de frontmatter) en per JSON-item
  (`uitsluit_veld`).
- Maskeren van wachtwoorden, tokens, sleutels en lange willekeurige reeksen, standaard
  aan.
- Verslag na elke bouw; index-bestand alleen leesbaar voor de eigenaar (Linux/macOS).

**Bouwen en beheer**
- Embedding-cache: een herbouw berekent alleen gewijzigde stukken opnieuw; de
  modelnaam zit in de cachesleutel.
- Atomisch bouwen: nooit een halve index actief.
- De index onthoudt model, dimensie en versie; zoeken met een afwijkende config wordt
  geweigerd met een duidelijke melding.
- Offline gebruik via `model_pad`; `polaris model` haalt het model vooraf op.

**Opdrachten**
- `bouw`, `zoek` (met `-k` en `--json`), `info`, `model`, `versie`, `bijwerken`.
- Bijwerken via git (`polaris bijwerken`); `pakket.py` bouwt een zip uit een
  release-tag.

**Overig**
- Tests zonder model of netwerk (`python -m unittest discover -s tests`).
- Demo-kennisbank in `voorbeelden/demo`, die ook de beveiliging laat zien.
- Documentatie: `docs/techniek.md`, `docs/beveiliging.md`.

**Bekende beperkingen**
- Elke zoekopdracht laadt het model opnieuw (2-3 s). Een warme zoekservice staat op de
  planning.
- Geen evaluatieset: de parameters zijn verstandige standaarden, nog niet geijkt.

## Voorgeschiedenis (vóór 0.1.0)

Polaris is ontstaan als los zoekscript en in een paar stappen naar deze vorm gegroeid.
Die stappen verklaren een aantal keuzes:

1. **Substring-zoeken** in bestanden en metadata. Werkte voor exacte termen, maar vond
   niets zodra een vraag anders geformuleerd was dan de tekst.
2. **Hybride index** — woordindex plus embeddings met RRF. Vond voor het eerst stukken
   op betekenis waar geen enkel woord overeenkwam.
3. **Groter model.** Van e5-small naar e5-base: op gewone laptophardware kost dat alleen
   bij de eerste bouw meer tijd (~60 s → ~200 s voor 772 stukken); dankzij de cache
   blijven latere bouwen snel.
4. **Live-referenties**, zodat een aanroepend programma weet wanneer een momentopname
   niet genoeg is.
5. **Losgemaakt en configureerbaar gemaakt** — geen vaste paden meer, alles via
   `polaris.toml` — en voorzien van versiebeheer, beveiliging, tests en documentatie.
   Dat werd 0.1.0.
