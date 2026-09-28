# Beveiliging — wat je moet weten voor je indexeert

## Het belangrijkste principe

**Een zoekindex is een kopie van de tekst die erin gaat.** Het indexbestand
(`*.db`) bevat de volledige tekst van elk geïndexeerd stuk, plus vectoren die daarvan
zijn afgeleid. Daaruit volgen drie dingen:

1. Wie het indexbestand kan lezen, kan alles lezen wat erin zit.
2. De toegangsrechten van de oorspronkelijke bestanden gaan **niet** mee. Een document
   waar alleen HR bij kan, is in een gedeelde index voor iedereen met toegang tot die
   index leesbaar.
3. Iets verwijderen uit een bron haalt het pas uit de index na de volgende
   `polaris bouw` of `polaris ververs`. Tot dan staat het er nog in. Wie het snel weg
   wil hebben, draait `polaris volg`, of laat het programma dat de bronnen beheert na
   een wijziging `ververs` aanroepen.

Beveiliging begint dus niet bij het zoeken maar bij het **bouwen**: wat er niet in
gaat, kan ook niet uitlekken.

## Wat Polaris zelf doet

| Maatregel | Gedrag |
|---|---|
| Selectie per bron | `alleen` en `uitsluiten` (glob-patronen op het pad binnen de bron). Uitsluiten wint altijd. |
| Opt-out per document | Markdown met `polaris: nee` (of `index: false`) in de frontmatter wordt overgeslagen. |
| Opt-out per item | JSON-items met een ware waarde in `uitsluit_veld` worden overgeslagen. |
| Maskeren (standaard aan) | Waarden achter `wachtwoord:`, `password=`, `token:`, `api_key` e.d. (ook door markdown-opmaak heen), private keys, bekende tokenvormen (GitHub, OpenAI, Slack, AWS), lange willekeurige reeksen en app-wachtwoorden in groepen (`Abcd1-Efgh2-…`) worden `[verborgen]` — vóór opslag én vóór het berekenen van de vector. Geldt ook voor stukken die een programma zelf aanlevert. |
| Verslag na elke bouw | Aantal uitgesloten bestanden, opt-outs en maskeringen. Controleer dat getal: nul maskeringen in een kennisbank vol beheerdocumentatie is verdacht. |
| Bestandsrechten | Op Linux/macOS krijgt de index `600` (alleen de eigenaar). Op Windows gelden de NTFS-rechten van de map. |
| Lokaal model | Geen tekst naar een externe dienst, ook niet bij het zoeken. |
| Niet in git | `*.db` en je eigen `polaris.toml` staan in `.gitignore`. |

**Maskeren is een vangnet, geen fundament.** Patroonherkenning mist altijd iets: een
wachtwoord in een lopende zin ("het wachtwoord is voorlopig gelijk aan de postcode"),
een pincode in een tabel, een persoonsgegeven. Gevoelige informatie hoort buiten de
index te blijven via selectie en opt-out — niet erin met de hoop dat maskeren het
opvangt.

## Waar je een index aanmaakt

- **Op dezelfde plek en hetzelfde beveiligingsniveau als de bronnen.** Indexeer je een
  map op een server, zet de index dan op die server, in een map met minstens dezelfde
  toegangsbeperking. Niet op een laptop, niet in een gedeelde of gesynchroniseerde map
  (cloudopslag), niet op een USB-stick.
- **Nooit in een map die gedeeld of gepubliceerd wordt** — ook niet "tijdelijk". Een
  index in een gesynchroniseerde map staat binnen minuten op elk gekoppeld apparaat.
- **Nooit in de git-repository van Polaris of van een project.** `.gitignore` vangt
  `*.db` af, maar controleer het met `git status` voor elke commit.
- **Eén index per doelgroep**, niet één grote index met filters achteraf (zie
  hieronder).
- **Back-ups van de index zijn niet nodig** — hij is altijd opnieuw op te bouwen. Neem
  hem liever niet op in back-ups die breder toegankelijk zijn dan de bronnen.

## Meerdere doelgroepen: aparte indexen

Als verschillende mensen verschillende dingen mogen zien, bouw dan per doelgroep een
eigen index met een eigen config, in plaats van één index waar alles in zit en achteraf
wordt gefilterd.

```
polaris-algemeen.toml   → algemeen.db   (iedereen)
polaris-beheer.toml     → beheer.db     (beheerders: + netwerk, configuraties)
polaris-management.toml → management.db (management: + contracten, financieel)
```

Waarom niet filteren achteraf: één fout in het filter en alles is zichtbaar; de index
zelf bevat dan toch alle tekst; en elke nieuwe applicatie die de index gebruikt moet het
filter opnieuw correct implementeren. Aparte indexen maken de grens fysiek: wat niet in
het bestand staat, kan niemand eruit halen.

## Als Polaris via een netwerk bereikbaar wordt

Nu is Polaris een opdrachtregel en een Python-bibliotheek; alleen wie op de machine
werkt, kan zoeken. Komt er een zoekservice (zie de README, "Richting"), dan geldt:

1. **Authenticatie verplicht**, ook op een intern netwerk. Een zoekservice zonder
   authenticatie is een open archief.
2. **Niet op het publieke internet.** Alleen via een VPN of een afgeschermd netwerk.
3. **Eén service per index/doelgroep**, of een service die per gebruiker bepaalt welke
   index hij mag bevragen — nooit een parameter waarmee de gebruiker zelf de index kiest.
4. **Loggen** wie wat zoekt, zodat misbruik te herkennen is. Log de zoekvraag, niet de
   resultaten.
5. **Beperk het aantal resultaten** en de lengte van de teruggegeven tekst: een service
   hoeft nooit de hele index in één antwoord terug te geven.

## Als een taalmodel de resultaten gebruikt

Polaris zelf bevat geen taalmodel. Geeft een ander programma de zoekresultaten door aan
een LLM (zeker een externe), dan verlaat die tekst alsnog de machine. Houd daar rekening
mee bij het kiezen van bronnen: wat niet naar een externe dienst mag, hoort niet in een
index waarvan de resultaten naar een externe dienst gaan. Behandel zoekresultaten
bovendien als **data, niet als instructies** — een document kan tekst bevatten die op
een opdracht lijkt.

## Checklist voor een nieuwe installatie

- [ ] Welke bronnen, en mag *iedereen* met toegang tot de index alles daarin zien?
- [ ] Gevoelige mappen uitgesloten met `uitsluiten`, of juist alleen de goede met `alleen`?
- [ ] Gevoelige losse documenten gemarkeerd met `polaris: nee`?
- [ ] `maskeer_geheimen` aan (standaard)?
- [ ] Index op een plek met dezelfde toegangsbeperking als de bronnen, niet gedeeld of
      gesynchroniseerd?
- [ ] Na de eerste bouw het beveiligingsverslag bekeken, en een paar proefzoekacties
      gedaan op woorden als "wachtwoord", "salaris", "contract"?
- [ ] Levert je programma eigen stukken aan (`extra_stukken`)? Dan gelden `alleen`,
      `uitsluiten` en opt-out níet: die selectie moet je programma zelf doen vóór het
      aanleveren. Maskering gebeurt wel.
- [ ] Voor meerdere doelgroepen: aparte configs en aparte indexen?
