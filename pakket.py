# -*- coding: utf-8 -*-
"""pakket.py - bouwt een verstuurbaar archief van Polaris: dist/polaris-<versie>.zip.

Gebruikt `git archive` op de tag die bij VERSION hoort, zodat het zip-bestand altijd
precies overeenkomt met een vastgelegde commit - geen los bij te houden bestandenlijst
die kan schuiven ten opzichte van wat er echt in de repo staat. Modelbestanden zitten
er nooit in (die zijn geen deel van de repo); die worden bij eerste gebruik gedownload.

    python pakket.py

Vereist dat de huidige VERSION ook als git-tag bestaat (v<versie>) - zie
"Een nieuwe versie uitbrengen" in README.md.
"""
import os
import subprocess
import sys

HIER = os.path.dirname(os.path.abspath(__file__))


def versie():
    with open(os.path.join(HIER, "VERSION"), encoding="utf-8") as f:
        return f.read().strip()


def main():
    v = versie()
    tag = "v%s" % v
    r = subprocess.run(["git", "-C", HIER, "tag", "-l", tag], capture_output=True, text=True)
    if tag not in r.stdout.split():
        print("Tag %s bestaat nog niet. Commit eerst, tag dan met:" % tag)
        print("  git -C %s tag %s" % (HIER, tag))
        sys.exit(1)

    dist = os.path.join(HIER, "dist")
    os.makedirs(dist, exist_ok=True)
    doel = os.path.join(dist, "polaris-%s.zip" % v)

    r = subprocess.run(
        ["git", "-C", HIER, "archive", "--format=zip", "--prefix=polaris-%s/" % v,
         "-o", doel, tag],
        capture_output=True, text=True)
    if r.returncode != 0:
        print("git archive is mislukt:")
        print(r.stderr)
        sys.exit(1)

    print("pakket gebouwd: %s (uit tag %s)" % (doel, tag))
    print("Uitpakken en installeren met: pip install -r requirements.txt")
    print("Voor toekomstig bijwerken via `polaris bijwerken`: laat de ontvanger in plaats "
          "van het zip-bestand liever `git clone` gebruiken op de centrale repo.")


if __name__ == "__main__":
    main()
