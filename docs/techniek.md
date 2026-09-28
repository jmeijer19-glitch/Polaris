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

**Frontmatter** (het `---`-blok bovenaan een markdown-bestand) wordt niet als tekst
geïndexeerd maar gebruikt: `description` gaat als eerste alinea vóór élk stuk van dat
document. Een sectie "Controle" op zichzelf zegt niets; met "Back-upprocedure van de
server" ervoor weet zowel BM25 als het model waar hij bij hoort. `status`, `title` en
`date` worden overgenomen.

**JSON-lijsten** worden één stuk per item: titel plus tekstveld, of met `alle_velden`
elk veld als `sleutel: waarde`-regel (accolades en aanhalingstekens zijn ruis voor
zowel BM25 als het model). Items zijn al een natuurlijke eenheid; verder knippen haalt
de samenhang weg. Daarnaast krijgt elke lijst één **overzichtsstuk** met alle namen,
sectie en status. Een vraag van het type "welke … zijn er" heeft geen enkel los item
als antwoord; zonder overzichtsstuk vond zo'n vraag in de praktijk 3 van 27 items.

**Ontdubbelen.** Stukken met (na normaliseren van witruimte) exact dezelfde tekst gaan er
één keer in. Anders domineren kopieën de resultaten.

## 3. De woordindex (FTS5)

SQLite's ingebouwde full-text search, met `tokenize="unicode61 remove_diacritics 2"`
zodat "één" en "een", "café" en "cafe" gelijk zijn.

**Ranking: BM25 met kolomgewichten** — titel ×6, sectie ×3, tekst ×1. Een term in de
titel zegt veel meer over waar een stuk over gaat dan dezelfde term ergens in de lopende
tekst.

**Drie pogingen, aanvullend, met aflopend gewicht:**

| Poging | Voorbeeld voor "herstelprocedure firewall" | Gewicht in de fusie |
|---|---|---|
| Exact | `"herstelprocedure" "firewall"` | 1,0 |
| Alle stammen (AND) | `herstelproced* AND firewall` | 0,7 |
| Een van de stammen (OR) | `herstelproced* OR firewall` | 0,5 |

De pogingen **vullen elkaar aan**: vond de exacte poging maar twee stukken, dan komen
daar de stam-treffers achter, elk met het gewicht van hun poging. Eerder telde alleen
de eerste poging die iets vond, waardoor het beste stuk, dat net een ander woord
gebruikte, buiten beeld bleef zodra er ergens één exacte treffer was.

De **stam** is grof: woorden van 7+ tekens verliezen hun laatste 3 letters en krijgen een
wildcard, zodat "betalen" ook "betaald" en "betaling" vindt. Een echte Nederlandse
stemmer zou preciezer zijn, maar dit dekt de meeste vervoegingen zonder extra
afhankelijkheid. Hoe ruimer de poging, hoe lager het gewicht: een ruime treffer is
minder zeker. De FTS-tabel heeft een prefix-index (`prefix='3 5'`), zodat zo'n
wildcard een indexlookup is en geen scan.

**Nederlandse stemmer als optie** (`stemmer = "nl"`). De Snowball-stemmer voor
Nederlands is preciezer dan afkappen, maar zijn stammen zijn geen voorvoegsels van het
woord: "betalingen" en "betaald" worden allebei "betaal", "storing" wordt "stoor",
"controle" wordt "controol". Een wildcard op zo'n stam vindt dus niets. Daarom krijgt
de index een vijfde FTS-kolom met de stammen van titel, sectie en tekst, en wordt de
vraag met dezelfde stemmer gestemd: stam tegen stam, exact, alleen op die kolom. De
exacte poging blijft op de gewone kolommen lopen. Kosten: een kleine pure-Python-
afhankelijkheid en een herbouw; de index onthoudt de stemmer en weigert een config die
een andere noemt. Standaard blijft "grof", zonder afhankelijkheid.

**Snippets** (`snippets = true`) komen uit SQLite's `snippet()` op de tekstkolom: de
zin rond de gevonden woorden, gemarkeerd. Een treffer via de betekenislijst of via de
stam-kolom heeft daar geen gemarkeerd woord en krijgt het begin van de tekst.

**Eigen syntax gaat voor.** Bevat de vraag FTS5-syntax (`"…"`, `*`, `AND`, `OR`,
`NOT`, `NEAR`), dan weet de vraagsteller precies wat hij zoekt: de vraag gaat
ongewijzigd naar de woordindex en de betekenislijst blijft achterwege.

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
| **Titel en kopjespad vóór de tekst** | De invoer is `Titel › Sectie\ntekst`. Zonder die kop krijgt een losse sectie een vector die nergens over gaat. De cache-sleutel is de volledige invoer, dus een titelwijziging telt als wijziging. |
| **Expliciet aantal threads** | `threads` in de config, standaard alle kernen. Zonder expliciete waarde probeert ONNX Runtime threads aan kernen te pinnen, wat in containers zonder cpu-affinity een foutmelding per sessie geeft. |
| **Batches op lengte gesorteerd** | Een batch wordt opgevuld tot zijn langste tekst; korte stukken naast één lange kosten evenveel als allemaal lange. Sorteren scheelde in de praktijk ruim een derde van de bouwtijd. |

**Gemeten** op een laptop-CPU (12 threads), 772 stukken: volledige bouw met e5-small
~60 s, met e5-base ~160-200 s. Op een kleine server met twee kernen doet e5-small
~6 stukken per seconde. Een herbouw is sneller dankzij de cache (zie §6).

**Warm houden.** Het laden van tokenizer en ONNX-sessie kost 1-3 s, het zoeken zelf
milliseconden. De `Zoeker` laadt daarom één keer en houdt model én vectormatrix in het
geheugen; hij controleert bij elke vraag de bestandsdatum en -grootte van de index en
leest de matrix alleen opnieuw als die veranderd zijn. Een langlopend proces betaalt
zo per vraag alleen het zoeken. De CLI is per definitie koud; daar helpt alleen een
zoekservice (zie de README, "Richting").

## 5. Samenvoegen: Reciprocal Rank Fusion

Elke lijst levert zijn top-40. Elk stuk krijgt:

```
score = Σ  gewicht / (60 + rang)
```

over de lijsten waarin het voorkomt (woordlijst met gewicht 1,0 / 0,7 / 0,5 naar
gelang de poging, betekenislijst met 1,0, en een lijst per naam uit de vraag met
`namen_gewicht`, zie §5b).

**Waarom RRF en niet de scores optellen?** BM25-scores zijn onbegrensd en hangen af van
de verzameling; cosinusscores liggen tussen -1 en 1. Die direct optellen vraagt om
kalibratie die per kennisbank anders uitvalt. RRF kijkt alleen naar de *rangorde* en
is daardoor robuust zonder afstemming. De constante 60 is de gangbare standaardwaarde
uit de literatuur; hij dempt het verschil tussen plek 1 en plek 2.

Een stuk dat in **beide** lijsten hoog staat, wint — precies het gedrag dat je wilt:
het klopt zowel letterlijk als inhoudelijk.

## 5b. Namen in de vraag

**Het probleem.** De betekenislijst vindt stukken die *over hetzelfde onderwerp* gaan.
Vraag je "wie neemt Bakkerij Vermeulen over tijdens de afwezigheid van Karin", dan staan
stukken over afwezigheid en opvolging bovenaan, en zakt een stuk dat over iets anders
gaat maar de klant noemt weg. In de praktijk: op alleen de klantnaam stond het goede stuk
op plek 1, met de hele vraag op plek 12.

**Herkennen.** Een naam is een woord met een hoofdletter dat niet vooraan een zin staat,
of een woord helemaal in hoofdletters (een afkorting). Aaneengesloten namenwoorden zijn
één naam ("Bakkerij Vermeulen"). Vraagwoorden en stopwoorden tellen niet. Van een
samenstelling tellen alleen de delen met een hoofdletter of cijfer: in
"Noordwind-contract" is "Noordwind" de naam, "TK-1" blijft heel. Dat laatste scheelde
meetbaar: met "noordwind contract" als frase zakte het contract-document van plek 2 naar
plek 4.

**Zoeken.** Per naam een woordlijst met de naam als exacte frase op titel, sectie en
tekst (niet de stam-kolom), en bij twee of meer namen nog een lijst met de stukken die ze
allemaal bevatten. Die lijsten gaan met `namen_gewicht` de fusie in. Een stuk met álle
namen staat zo in meerdere lijsten en krijgt vanzelf de bonus. FTS5-syntax in de vraag
schakelt dit uit, net als de betekenislijst.

**Het gewicht** (standaard 0,7) is gemeten op een kennisbank van 4.414 stukken met een
evaluatieset van 20 vragen: zonder namen MRR 0,67, met 0,5 → 0,68, met 0,7 en 1,0 → 0,71,
met 1,5 → 0,61 en een vraag minder gevonden. Bij te veel gewicht duwt een naam die in
veel stukken staat het goede stuk weg. Een vraag in kleine letters heeft geen
namen en zoekt precies zoals vóór 0.4.0.

## 6. Herrangschikking en cache

**Status.** Stukken met status `klaar`, `afgerond`, `gesloten`, `vervallen`,
`vervangen`, `done`, `closed` of `deprecated` krijgen ×0,5. Ze blijven vindbaar ("wat
hebben we toen besloten"), maar zakken onder wat nog actueel is.

**Actualiteit.** Elk stuk krijgt er `actualiteit_bonus × (score van een eerste plek) ×
versheid` bij, waarbij versheid 1,0 is voor vandaag en lineair naar 0 loopt op
`actualiteit_dagen` (standaard 730). Met de standaard 0,25 is dat hoogstens een kwart
van een eerste plek: genoeg om bij twee even goede treffers de nieuwste te laten
winnen, te weinig om een slechtere treffer omhoog te duwen. Bewust klein: relevantie
blijft leidend. De datum komt uit de frontmatter, het pad, de eerste regels of de
bestandsdatum; zie de beperkingen.

**Bronfilter.** `zoek(..., bron=…)` beperkt alle lijsten tot één bron: de woordlijsten
via een subquery op de FTS-tabel, de betekenislijst via een masker op de matrix.

**Aanvullen per document.** Na de top-k: per document (zelfde bron en pad) dat erin
staat, zoekt Polaris de andere stukken van dat document die de namen uit de vraag
bevatten, of zonder namen een van de woorden. Alleen in sectie en tekst: de titel delen
alle stukken van een document, dus een naam in de titel zou elk stuk raak maken. Hoogstens
`aanvullen` (standaard 2) per document, gerangschikt op BM25, en dan in documentvolgorde
(`volgnr`) bij de hoogste treffer van dat document, in het veld `ook_in_dit_document`.
Stukken die zelf al in de top-k staan, komen er niet dubbel in.

Aanleiding: een document met een samenvatting ("Ruben blijft bij Bakkerij Vermeulen") en
verderop een tabel ("Bakkerij Vermeulen → Yusuf"). De vraag raakte de samenvatting; de
tabel viel buiten de top-k, en het antwoord werd gebouwd op de helft die de andere helft
tegensprak. Met aanvullen ziet de lezer beide en kan hij de tegenspraak noemen. Het is een
veld bij de treffer in plaats van extra treffers, zodat één lang document de lijst niet
opvult en de rangorde (en de MRR) niet verandert.

**Embedding-cache.** Per stuk wordt de vector opgeslagen onder
`sha1(modelnaam | embed-invoer)`. Bij een herbouw worden alleen stukken met nieuwe of
gewijzigde invoer opnieuw berekend. De modelnaam zit in de sleutel, zodat na een
modelwissel nooit oude vectoren worden hergebruikt. Cache-regels die geen enkel stuk
meer gebruikt, gaan bij de herbouw vanzelf weg: de nieuwe index krijgt alleen wat
gebruikt is.

**Controle bij het zoeken.** De index onthoudt met welk model, welke dimensie en welke
schemaversie hij is gebouwd. Wijkt de config of de code daarvan af, dan weigert `zoek`
met een duidelijke melding in plaats van stilletjes alleen op woorden of met verkeerde
vectoren te zoeken.

**Lock.** Rond een bouw ligt een lockbestand (`<index>.lock`, aangemaakt met
`O_EXCL`). Een tweede bouw krijgt `BouwBezig`; een lock ouder dan een uur geldt als
achtergebleven van een afgebroken proces en wordt opgeruimd. Zonder lock wisten een
cron-bouw en een handmatige bouw elkaars tmp-bestand.

**Atomisch bouwen.** Er wordt gebouwd in `<index>.tmp`, dat pas aan het eind met
`os.replace` de oude index vervangt. Een lezer ziet dus altijd een complete index, ook
als de bouw halverwege afbreekt.

**Verversen.** Een volledige bouw leest en knipt alle bronnen en schrijft de hele index
opnieuw, ook als de cache de embeddings levert. `ververs` werkt de bestaande index bij:

| Bron | Wat er vergeleken wordt |
|---|---|
| markdown | per bestand wijzigingstijd en grootte (tabel `bronbestand`); een gelijk bestand wordt niet gelezen, zijn stukken komen uit de index |
| JSON-lijst | het bestand wordt gelezen, daarna per item vergeleken; één gewijzigde taak is één gewijzigd stuk (plus het overzichtsstuk) |
| aangeleverde stukken | per stuk een vingerafdruk van het ruwe stuk; een gelijk stuk wordt niet opnieuw gemaskeerd |

Daarna per stuk-ID de vingerafdruk van wat er opgeslagen wordt (`inhoud`): nieuw of
anders gaat erin, verdwenen gaat eruit, en dat alles in één transactie over de
stuktabel, de FTS-tabel (via de gedeelde rowid) en de vectortabel. Een lezer ziet de oude
of de nieuwe stand, nooit iets ertussen. Embedden gebeurt vóór de transactie, zodat
zoeken intussen doorgaat. De warme `Zoeker` merkt de wijziging aan de bestandsdatum en
leest de matrix opnieuw.

**Het resultaat is gelijk aan een volledige bouw**, en de tests controleren dat. Het
lastige geval is ontdubbelen: twee bestanden met dezelfde tekst leveren één stuk op.
Verandert het bestand dat bleef, dan hoort het andere terug te komen. Daarom is een
bestand alleen te hergebruiken als al zijn stukken ongewijzigd in de index staan; anders
wordt het opnieuw gelezen. Kan verversen niet op de bestaande index voortbouwen (geen
index, een oudere indexstructuur, of een andere config of Polaris-versie volgens de
vingerafdruk in `meta`), dan wordt het een volledige bouw.

**Gemeten** op 4.414 aangeleverde stukken (zuinige mini-pc, 2 threads): volledige bouw 3,1 s,
verversen zonder wijzigingen 0,6 s, met één gewijzigd stuk 1,6 s, waarvan ~0,9 s het
koud laden van het model. In een proces dat het model warm houdt, is dat ruim onder de
seconde.

**Volgen.** `polaris volg` kijkt elke paar seconden naar wijzigingstijd en grootte van de
bronbestanden en ververst als het na een wijziging `--rust` seconden (standaard 20) stil
is. Het wachten vangt een editor die in stappen opslaat of een synchronisatie die veel
bestanden tegelijk neerzet. Stukken die een programma aanlevert, ziet `volg` niet; dat
programma roept zelf `ververs` aan wanneer het weet dat er iets veranderd is.

## 7. Eigen bronnen

Polaris kent twee brontypes, maar een kennisbank bestaat zelden alleen uit markdown en
JSON: commit-berichten, een live inventaris, rijen uit een database. In plaats van voor
elk daarvan een brontype in te bouwen, accepteert `bouw()` kant-en-klare stukken van het
aanroepende programma (`extra_stukken`). Die doorlopen dezelfde maskering, ontdubbeling
en embedding. Het programma kent zijn eigen bronnen het best; Polaris blijft de motor.

## 7b. Live-referenties

Elk stuk krijgt een veld `verwijzingen`: ID's of ticketnummers die erin genoemd worden
(standaardpatroon `AB-123`, uitbreidbaar met `verwijzingspatronen`). Polaris haalt zelf
niets live op — dat past niet bij "puur zoektechniek". Maar het aanroepende programma
weet zo wanneer een treffer over iets in een extern systeem gaat, en dat voor de
*actuele* stand daar beter gekeken kan worden dan in een index die een momentopname is.

## 8. Meten

`polaris eval` draait een lijst vragen met een verwacht resultaat (reguliere expressie
op pad of titel) en geeft per vraag de plek van de eerste goede treffer, plus de Mean
Reciprocal Rank over de set: gemiddelde van 1/plek, 0 voor niet gevonden. De maat is
bewust simpel. Hij beloont bovenaan staan, straft afwezigheid, en is met tien tot twintig
vragen al gevoelig genoeg om een verslechtering te zien. Een set die je zelf samenstelt
heeft een risico: de index wordt beter voor precies die vragen. Neem daarom vragen op
die je echt hebt gesteld, en een paar waarin het kernwoord niet letterlijk in de bron
staat. Eén run is geen ruis, want er zit geen taalmodel in: dezelfde index en dezelfde
vraag geven altijd dezelfde uitkomst.

Naast de plek is er een tweede maat: **compleet**. Een vraag met `verwacht_alle` noemt
alle stukken die mee moeten komen (op pad, titel of sectie), en telt als compleet als ze
er allemaal zijn, in de top-k of in `ook_in_dit_document`. Dat meet het aanvullen: in de
demo haalt de vraag over Bakkerij Vermeulen met `k = 1` zonder aanvullen 0/1 (de
samenvatting ontbreekt), met aanvullen 1/1.

## 9. Bekende beperkingen

1. **Koude start** van 1-3 s per CLI-aanroep (model laden). Binnen één proces lost de
   `Zoeker` dit op; over processen heen vraagt het een zoekservice.
2. **Grove stam** standaard en een handgemaakte stoplijst; de Snowball-stemmer is een
   optie, geen volledige taalanalyse. De namenherkenning is even eenvoudig: een naam
   vooraan een zin wordt gemist, en twee namen direct achter elkaar ("Ruben Yusuf") worden
   één naam.
3. **Geen reranker** (cross-encoder); de volgorde komt uit RRF plus status- en
   actualiteitsweging.
4. **Datum** komt uit frontmatter, pad, de eerste regels tekst of de bestandsdatum — de
   bestandsdatum is niet altijd het moment waarop de inhoud gold, en dan is de
   actualiteitsbonus een gok. Zet `actualiteit_bonus = 0` als je bronnen geen
   betrouwbare datum hebben.
5. **Parameters** (300/1.800 tekens, RRF 60, gewichten, bonus 0,25) zijn verstandige
   standaarden, overgenomen uit een kennisbank waar ze met een evaluatieset zijn
   bijgesteld. Met `polaris eval` kun je ze nu op je eigen kennisbank controleren.
6. **Het vervallen-signaal** werkt alleen zo goed als de bronnen een status bijhouden.
7. **Aanvullen en frontmatter.** De `description` uit de frontmatter staat vóór elk stuk
   van een document. Noemt die een naam uit de vraag, dan telt elk stuk van dat document
   als raak voor het aanvullen, en beslist alleen BM25 welke er meekomen.
