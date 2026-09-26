# Back-up en herstel

## Wat er wordt geback-upt

- Bestandsserver: elke nacht om 01:00, 30 dagen bewaard.
- Mailboxen en gedeelde documenten in de cloud: drie keer per dag.
- Configuratie van firewall en switches: wekelijks, na elke wijziging handmatig.

## Controle

Een back-up die nooit is teruggezet, is een aanname en geen back-up. Zet daarom elk
kwartaal één willekeurig bestand en één volledige map terug naar een testlocatie, en
noteer hoe lang dat duurde.

Let op: een status "voltooid" in het back-upprogramma zegt niet dat de laatste sessie
recent is. Kijk naar de datum van de laatste geslaagde sessie, niet naar het label.

## Herstel na een storing

1. Stel vast wat er mist en sinds wanneer.
2. Kies het laatste herstelpunt van vóór het probleem.
3. Zet eerst terug naar een aparte locatie en controleer de inhoud.
4. Pas daarna terugzetten naar de oorspronkelijke plek.

Meld elk herstel in het ticketsysteem, bijvoorbeeld als TCK-1042, zodat later terug te
vinden is wat er is teruggezet en waarom.
