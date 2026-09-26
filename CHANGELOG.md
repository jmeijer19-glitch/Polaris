# Changelog — Polaris

Alle betekenisvolle wijzigingen per versie, nieuwste bovenaan. Versienummers volgen
[semantic versioning](https://semver.org/lang/nl/): een verhoging van het middelste
getal betekent nieuwe functionaliteit, van het laatste getal een reparatie. Staat er
**index opnieuw bouwen** bij een versie, draai dan na het bijwerken `polaris bouw`.

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
