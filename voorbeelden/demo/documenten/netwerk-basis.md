# Netwerk — basisinrichting kantoor

## Overzicht

Het kantoornetwerk bestaat uit een firewall, één centrale switch met PoE en drie
access points. Gasten krijgen een apart wifi-netwerk zonder toegang tot interne
apparaten.

## VLAN-indeling

| VLAN | Doel | Subnet |
|---|---|---|
| 10 | Werkplekken | 10.0.10.0/24 |
| 20 | Printers en scanners | 10.0.20.0/24 |
| 30 | Gasten | 10.0.30.0/24 |
| 99 | Beheer | 10.0.99.0/24 |

Het beheer-VLAN is alleen bereikbaar vanaf de beheerwerkplek en via de VPN.

## Access points

De access points krijgen stroom via de switch (PoE). Valt een access point uit, kijk
dan eerst of de poort op de switch nog stroom levert voordat je het apparaat vervangt.
Een losse voedingsadapter is alleen nodig als de switch geen PoE-budget meer over heeft.

## Beheertoegang

Beheerder: netadmin
Wachtwoord: Zomer2026!Kantoor

Dit wachtwoord staat hier alleen om te laten zien dat Polaris het maskeert voordat het
in de index komt — zoek maar op "wachtwoord", je vindt de regel wel, de waarde niet.
