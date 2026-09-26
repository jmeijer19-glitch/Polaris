# -*- coding: utf-8 -*-
"""polaris.update - bijwerken naar een nieuwe versie via git.

Het idee: één centrale geschiedenis (deze map als git-repo, met een tag per release),
en elke installatie is een clone daarvan. Bijwerken is dan `git pull` - geen los
mechanisme dat zelf bestanden moet gaan overschrijven en kan half mislukken.

Is de map geen git-checkout (iemand heeft alleen het zip-bestand uitgepakt), dan kan dit
niet automatisch - dat wordt uitgelegd in plaats van geraden.
"""
import os
import subprocess

from . import versie as huidige_versie


def _pakket_map():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _git(*args):
    return subprocess.run(["git", "-C", _pakket_map()] + list(args),
                           capture_output=True, text=True)


def is_git_checkout():
    r = _git("rev-parse", "--is-inside-work-tree")
    return r.returncode == 0 and r.stdout.strip() == "true"


def bijwerken():
    if not is_git_checkout():
        print("Dit is geen git-checkout van Polaris (waarschijnlijk alleen het "
              "zip-bestand uitgepakt), dus automatisch bijwerken kan niet.")
        print("Vraag een nieuw zip-bestand, of zet dit om naar een git-clone van de "
              "centrale versie om voortaan wel te kunnen bijwerken:")
        print("  git clone <adres van de centrale repo> polaris")
        return

    voor = huidige_versie()
    r = _git("pull", "--tags")
    if r.returncode != 0:
        if "no tracking information" in r.stderr.lower():
            tak = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main"
            print("Deze checkout heeft geen remote ingesteld om van te pullen. Is dit "
                  "de centrale repo zelf: dan is er niets om bij te werken. Is dit een "
                  "clone die de remote kwijt is: zet 'm terug met\n"
                  "  git remote add origin <adres van de centrale repo>\n"
                  "  git branch --set-upstream-to=origin/%s %s" % (tak, tak))
            return
        print("git pull is mislukt:")
        print(r.stderr.strip())
        return
    print(r.stdout.strip())

    # __init__.py leest VERSION dynamisch, maar deze module is al geladen - het bestand
    # opnieuw lezen geeft wel de juiste, verse waarde.
    na = huidige_versie()
    if na != voor:
        print("Polaris bijgewerkt: %s -> %s" % (voor, na))
        print("Vergeet niet: pip install -r requirements.txt als er nieuwe "
              "afhankelijkheden bij zijn gekomen, en `polaris bouw` om de index opnieuw "
              "te bouwen als de indexstructuur is gewijzigd (zie CHANGELOG.md).")
    else:
        print("Al bij: versie %s is de nieuwste." % na)
