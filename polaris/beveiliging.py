# -*- coding: utf-8 -*-
"""polaris.beveiliging - wat er wel en niet in de index terechtkomt.

Een zoekindex is een kopie van de tekst die erin gaat. Wie het indexbestand kan lezen,
kan alles lezen wat erin geïndexeerd is - de toegangsrechten van de oorspronkelijke
bestanden gaan níet mee. Daarom beslist Polaris al bij het bouwen wat erin mag, in drie
lagen:

1. Selectie per bron (config): `alleen` en `uitsluiten` als glob-patronen op het pad.
2. Opt-out per document: een markdown-bestand met `polaris: nee` in de frontmatter, of
   een JSON-item met een waar `uitsluit_veld`, wordt overgeslagen.
3. Maskeren: wachtwoorden, tokens en sleutels die toch in een tekst staan, worden
   vervangen door [verborgen] voordat ze worden opgeslagen of ge-embed.

Laag 3 is een vangnet, geen vervanging voor 1 en 2: patroonherkenning mist altijd
iets. Zie docs/beveiliging.md voor de achtergrond en aanbevelingen.
"""
import fnmatch
import re

MASKER = "[verborgen]"

# Sleutel-waarde-paren waarvan de waarde geheim is: "wachtwoord: abc123",
# "password=abc", "api_key = xyz". De sleutelnaam blijft staan, zodat je nog kunt
# vinden dát er een wachtwoord gedocumenteerd is - alleen niet welk.
_SLEUTELWAARDE = re.compile(
    r"(?i)\b(wachtwoord|password|passwd|pwd|pincode|secret|geheim|token|"
    r"api[_\- ]?key|apikey|client[_\- ]?secret|access[_\- ]?key)"
    r"(\s*[:=]\s*)(\S+)")

_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")

# Bekende tokenvormen met een herkenbaar voorvoegsel.
_BEKENDE_TOKENS = re.compile(
    r"\b(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|"
    r"sk-[A-Za-z0-9_\-]{20,}|xox[abpr]-[A-Za-z0-9\-]{10,}|AKIA[0-9A-Z]{16})\b")

# Lange willekeurig ogende reeksen (hoofdletters, kleine letters en cijfers door elkaar,
# minstens 32 tekens) - typisch een sleutel of token zonder herkenbaar voorvoegsel.
_LANGE_REEKS = re.compile(r"\b[A-Za-z0-9+/_\-]{32,}={0,2}")

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*(\n|\Z)", re.S)
_OPTOUT = re.compile(r"(?im)^\s*(polaris|index)\s*:\s*(nee|no|false|uit|off)\s*$")

_WAAR = {"1", "true", "ja", "yes", "waar", "y", "j"}


def _lijkt_geheim(reeks):
    return (any(c.isdigit() for c in reeks) and any(c.islower() for c in reeks)
            and any(c.isupper() for c in reeks))


def maskeer(tekst):
    """Geeft (gemaskeerde tekst, aantal maskeringen) terug."""
    teller = [0]

    def _vervang_waarde(m):
        teller[0] += 1
        return m.group(1) + m.group(2) + MASKER

    def _vervang_heel(m):
        teller[0] += 1
        return MASKER

    def _vervang_reeks(m):
        if _lijkt_geheim(m.group(0)):
            teller[0] += 1
            return MASKER
        return m.group(0)

    tekst = _PRIVATE_KEY.sub(_vervang_heel, tekst)
    tekst = _SLEUTELWAARDE.sub(_vervang_waarde, tekst)
    tekst = _BEKENDE_TOKENS.sub(_vervang_heel, tekst)
    tekst = _LANGE_REEKS.sub(_vervang_reeks, tekst)
    return tekst, teller[0]


def heeft_optout(tekst):
    """True als een markdown-document in zijn frontmatter aangeeft niet geïndexeerd te
    willen worden (`polaris: nee` of `index: false`)."""
    m = _FRONTMATTER.match(tekst)
    return bool(m and _OPTOUT.search(m.group(1)))


def is_waar(waarde):
    if isinstance(waarde, bool):
        return waarde
    if waarde is None:
        return False
    return str(waarde).strip().lower() in _WAAR


def _past(rel, patroon):
    naam = rel.rsplit("/", 1)[-1]
    return fnmatch.fnmatch(rel, patroon) or fnmatch.fnmatch(naam, patroon)


def mag_indexeren(rel, alleen, uitsluiten):
    """Pad-selectie per bron. Uitsluiten wint altijd van alleen."""
    if any(_past(rel, p) for p in uitsluiten):
        return False
    if alleen and not any(_past(rel, p) for p in alleen):
        return False
    return True
