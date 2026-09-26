# Techniek — hoe Polaris zoekt, en waarom zo

Dit document legt elke technische keuze uit, met de reden en de afweging. Parameters
staan als constanten bovenin `polaris/index.py`.

## 1. Uitgangspunten

- **Lokaal.** Geen tekst naar een externe dienst. Het enige netwerkverkeer is het
  eenmalig ophalen van het model, en ook dat is te vermijden (`model_pad`).
- **Gewone hardware.** Geen GPU nodig; alles draait op de CPU van een laptop of kleine
  server.
- **Eén bestand, altijd opnieuw op te bouwen.** De index is een afgeleide van de
  bronnen, nooit iets dat je met de hand bewerkt. Kapot of verouderd? Opnieuw bouwen.
- **Bronnen verschillen per installatie, de motor niet.** Alles wat per kennisbank
  verschilt, staat in de config.

## 2. Chunking — hoe tekst in stukken gaat

Zoeken gebeurt per stuk, niet per bestand. Een heel document als één stuk levert vage
treffers op (het "gaat over van alles"); te kleine stukken missen hun context.

**Markdown** wordt geknipt op koppen `##` t/m `####`. Elk stuk krijgt het kopjespad als
sectienaam (`Back-up › Herstel na een storing`), zodat je ziet waar een treffer staat.

| Regel | Waarde | Waarom |
|---|---|---|
| Kleinste sectie | 300 tekens | Kortere secties gaan op in hun buur: een losse regel onder een kop heeft te weinig context om zinvol op te zoeken. |
| Grootste stuk | 1.800 tekens | Langere secties worden op alinea's geknipt. 1.800 tekens is voor Nederlands ongeveer 300-450 tokens — ruim binnen de 512 tokens die e5 aankan, met ruimte voor titel en sectie. |
| Koppen in codeblokken | genegeerd | Een `##` in een codevoorbeeld is geen hoofdstuk. |

**JSON-lijsten** worden één stuk per item: titel plus tekstveld. Items zijn al een
natuurlijke eenheid; verder knippen haalt de samenhang weg.

**Ontdubbelen.** Stukken met (na normaliseren van witruimte) exact dezelfde tekst gaan er
één keer in. Anders domineren kopieën de resultaten.

## 3. De woordindex (FTS5)

SQLite's ingebouwde full-text search, met `tokenize="unicode61 remove_diacritics 2"`
zodat "één" en "een", "café" en "cafe" gelijk zijn.

**Ranking: BM25 met kolomgewichten** — titel ×6, sectie ×3, tekst ×1. Een term in de
titel zegt veel meer over waar een stuk over gaat dan dezelfde term ergens in de lopende
tekst.

**Drie pogingen, de eerste die iets vindt telt:**

| Poging | Voorbeeld voor "herstelprocedure firewall" | Gewicht in de fusie |
|---|---|---|
| Exact | `"herstelprocedure" "firewall"` | 1,0 |
| Alle stammen (AND) | `herstelproced* AND firewall` | 0,7 |
| Een van de stammen (OR) | `herstelproced* OR firewall` | 0,5 |

De **stam** is grof: woorden van 7+ tekens verliezen hun laatste 3 letters en krijgen een
wildcard, zodat "betalen" ook "betaald" en "betaling" vindt. Een echte Nederlandse
stemmer zou preciezer zijn, maar dit dekt de meeste vervoegingen zonder extra
afhankelijkheid. Hoe ruimer de poging, hoe lager het gewicht: een ruime treffer is
minder zeker.

**Stopwoorden** ("de", "het", "wat", …) vallen weg; ze zitten in bijna elk stuk en
zeggen niets.

## 4. De betekenisindex (embeddings)

**Model:** `multilingual-e5-base` (of `-small`) van intfloat. Meertalig en sterk in
Nederlands voor zijn grootte; getraind op precies deze taak (vraag ↔ passage matchen).

| Keuze | Waarom |
|---|---|
| **int8-gekwantiseerd, ONNX** | Kwantisering maakt het model ~4× kleiner en sneller op de CPU, met verwaarloosbaar kwaliteitsverlies. ONNX Runtime heeft geen PyTorch nodig (honderden MB's minder). |
| **Voorvoegsels `query:` en `passage:`** | e5 is getraind met deze voorvoegsels; zonder presteert het merkbaar slechter. |
| **Mean pooling + L2-normalisatie** | De standaardmethode voor e5. Na normalisatie is cosinusgelijkenis gewoon een inproduct. |
| **Brute force zoeken** | Eén matrixvermenigvuldiging over alle vectoren. Bij duizenden tot tienduizenden stukken is dat milliseconden — een vectordatabase voegt dan alleen complexiteit toe. |
| **Max 384 tokens per stuk** | Past bij de stukgrootte; langere invoer wordt afgekapt. |

**Gemeten** op een laptop-CPU (12 threads), 772 stukken: volledige bouw met e5-small
~60 s, met e5-base ~160-200 s. Een herbouw is sneller dankzij de cache (zie §6). Eén
zoekopdracht kost ~2-3 s, vrijwel allemaal het laden van het model — het zoeken zelf is
milliseconden (zie "warme zoekservice" in de README).

## 5. Samenvoegen: Reciprocal Rank Fusion

Beide lijsten leveren hun top-40. Elk stuk krijgt:

```
score = Σ  gewicht / (60 + rang)
```

over de lijsten waarin het voorkomt (woordlijst met gewicht 1,0 / 0,7 / 0,5 naar
gelang de poging, betekenislijst met 1,0).

**Waarom RRF en niet de scores optellen?** BM25-scores zijn onbegrensd en hangen af van
de verzameling; cosinusscores liggen tussen -1 en 1. Die direct optellen vraagt om
kalibratie die per kennisbank anders uitvalt. RRF kijkt alleen naar de *rangorde* en
is daardoor robuust zonder afstemming. De constante 60 is de gangbare standaardwaarde
uit de literatuur; hij dempt het verschil tussen plek 1 en plek 2.

Een stuk dat in **beide** lijsten hoog staat, wint — precies het gedrag dat je wilt:
het klopt zowel letterlijk als inhoudelijk.

## 6. Herrangschikking en cache

**Status.** Stukken met status `klaar`, `afgerond`, `gesloten`, `vervallen`, `done` of
`closed` krijgen ×0,5. Ze blijven vindbaar ("wat hebben we toen besloten"), maar zakken
onder wat nog actueel is.

**Embedding-cache.** Per stuk wordt de vector opgeslagen onder
`sha1(modelnaam | tekst)`. Bij een herbouw worden alleen stukken met nieuwe of
gewijzigde tekst opnieuw berekend. De modelnaam zit in de sleutel, zodat na een
modelwissel nooit oude vectoren worden hergebruikt.

**Controle bij het zoeken.** De index onthoudt met welk model en welke dimensie hij is
gebouwd. Wijkt de config daarvan af, dan weigert `zoek` met een duidelijke melding in
plaats van stilletjes alleen op woorden te zoeken.

**Atomisch bouwen.** Er wordt gebouwd in `<index>.tmp`, dat pas aan het eind met
`os.replace` de oude index vervangt. Een lezer ziet dus altijd een complete index, ook
als de bouw halverwege afbreekt.

## 7. Live-referenties

Elk stuk krijgt een veld `verwijzingen`: ID's of ticketnummers die erin genoemd worden
(standaardpatroon `AB-123`, uitbreidbaar met `verwijzingspatronen`). Polaris haalt zelf
niets live op — dat past niet bij "puur zoektechniek". Maar het aanroepende programma
weet zo wanneer een treffer over iets in een extern systeem gaat, en dat voor de
*actuele* stand daar beter gekeken kan worden dan in een index die een momentopname is.

## 8. Bekende beperkingen

1. **Koude start** van 2-3 s per zoekopdracht (model laden). Oplossing: warme service.
2. **Grove stam** en een handgemaakte stoplijst, geen echte Nederlandse taalanalyse.
3. **Geen reranker** (cross-encoder); de volgorde komt uit RRF plus statusweging.
4. **Datum** komt uit het pad, de eerste regels tekst of de bestandsdatum — niet altijd
   het moment waarop de inhoud gold. Er is nog geen actualiteitsbonus.
5. **Parameters** (300/1.800 tekens, RRF 60, gewichten) zijn verstandige standaarden,
   nog niet geijkt met een evaluatieset.
