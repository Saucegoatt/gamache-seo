"""
Moteur Intentions SEO v2 — s'ancre sur de VRAIES recherches Google (grounding),
pense pour les clients SANS Search Console/Analytics (ex. nouveau client en refonte).
  Etape 1 : Gemini + Google Search -> donnees reelles selon contexte + region.
  Etape 2 : structuration en JSON (clusters + briefs + sources a brancher).
Pur stdlib + token gcloud.

Usage (Cloud Shell) :
    python3 seo_engine.py "https://www.excavationp2p.ca/" "Monteregie"
    python3 seo_engine.py "P2P Excavation, excavation residentielle" "Rive-Sud"
"""
import sys
import re
import json
import subprocess
import urllib.request
from urllib.parse import urlparse
from urllib.error import HTTPError

PNUM = "43644610505"
REGION_GCP = "us-central1"
MODEL = "gemini-2.5-pro"
BASE = (f"https://{REGION_GCP}-aiplatform.googleapis.com/v1/projects/{PNUM}"
        f"/locations/{REGION_GCP}/publishers/google/models/{MODEL}:generateContent")


def token():
    return subprocess.check_output(["gcloud", "auth", "print-access-token"]).decode().strip()


def call(body):
    req = urllib.request.Request(
        BASE, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json",
                 "X-Goog-User-Project": PNUM}, method="POST")
    with urllib.request.urlopen(req, timeout=240) as r:
        return json.loads(r.read().decode())


def text_of(data):
    try:
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts)
    except Exception:
        return ""


def fetch_site(url):
    p = urlparse(url if url.startswith("http") else "https://" + url)
    root = f"{p.scheme}://{p.netloc}/"
    try:
        req = urllib.request.Request(root, headers={"User-Agent": "Mozilla/5.0 GamacheSEO"})
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read(400000).decode("utf-8", "replace")
    except Exception as exc:
        print("  (lecture du site impossible :", str(exc)[:80], ")")
        return "", p.netloc
    html = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()[:5000], p.netloc


def ground(client, region, context):
    prompt = (
        "Tu es un strategiste SEO senior au Quebec. En t'appuyant sur de VRAIES "
        "recherches Google (sers-toi de l'outil de recherche), identifie les "
        "intentions de recherche pertinentes pour ce client dans SA region.\n\n"
        f"Client : {client}\nRegion : {region}\nContexte (site) : {context}\n\n"
        "Donne les requetes reelles que les gens tapent, les questions associees "
        "(People Also Ask), les variations locales et ce qui ressort vraiment des "
        "resultats Google. Couvre tout l'entonnoir, avec un fort accent LOCAL."
    )
    data = call({"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                 "tools": [{"googleSearch": {}}],
                 "generationConfig": {"temperature": 0.4, "maxOutputTokens": 8192}})
    gm = (data.get("candidates", [{}])[0].get("groundingMetadata") or {})
    return text_of(data), gm.get("webSearchQueries", [])


def structure(client, region, raw):
    prompt = (
        "Structure l'analyse SEO suivante en JSON exploitable.\n\n"
        f"Client : {client}\nRegion : {region}\nAnalyse brute :\n{raw}\n\n"
        "Format JSON STRICT : {\"client\":\"...\",\"region\":\"...\","
        "\"note_donnees\":\"<1 phrase: analyse basee sur le site + recherches Google "
        "publiques, pas encore de Search Console/Analytics car nouveau client>\","
        "\"sources_recommandees\":[\"Search Console (requetes reelles + positions)\","
        "\"GA4 (conversions par page)\",\"Token Google Ads (volumes de recherche)\"],"
        "\"clusters\":[{\"nom\":\"...\",\"intention\":\"informationnel|commercial|"
        "transactionnel|local\",\"priorite\":\"haute|moyenne|basse\",\"requetes\":"
        "[\"...\"],\"brief\":{\"titre\":\"...\",\"angle\":\"...\",\"faq\":[\"...\","
        "\"...\",\"...\"]}}]} . Donne 6 a 9 clusters. Reponds UNIQUEMENT le JSON."
    )
    data = call({"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                 "generationConfig": {"temperature": 0.3, "maxOutputTokens": 8192,
                                      "responseMimeType": "application/json"}})
    return text_of(data)


def parse(txt):
    txt = re.sub(r"^```(json)?\s*", "", txt.strip())
    txt = re.sub(r"\s*```$", "", txt).strip()
    try:
        return json.loads(txt)
    except Exception:
        m = re.search(r"\{.*\}", txt, re.S)
        return json.loads(m.group(0)) if m else {}


arg = sys.argv[1] if len(sys.argv) > 1 else "P2P Excavation, excavation et terrassement"
region = sys.argv[2] if len(sys.argv) > 2 else "Quebec (a deduire)"

if arg.startswith("http"):
    print("Lecture du site (page d'accueil) ...")
    context, client = fetch_site(arg)
    context = context or arg
else:
    context, client = arg, arg.split(",")[0].strip()

print(f"1/2  Recherche Google ancree pour : {client}  (region : {region})")
try:
    raw, searches = ground(client, region, context)
except HTTPError as e:
    print("ERREUR (grounding) :", e.code, e.read().decode("utf-8", "replace")[:300])
    sys.exit(1)
if searches:
    print("     Vraies recherches Google consultees :", " | ".join(searches[:8]))

print("2/2  Structuration ...")
try:
    result = parse(structure(client, region, raw))
except HTTPError as e:
    print("ERREUR (structure) :", e.code, e.read().decode("utf-8", "replace")[:300])
    sys.exit(1)

clusters = result.get("clusters", [])
with open("seo_result.json", "w", encoding="utf-8") as f:
    json.dump({**result, "recherches_google": searches}, f, ensure_ascii=False, indent=2)

print(f"\nClient : {result.get('client', client)}  |  Region : {result.get('region', region)}")
if result.get("note_donnees"):
    print("Note :", result["note_donnees"])
print(f"\n{len(clusters)} clusters :\n")
for c in clusters:
    reqs = c.get("requetes", [])
    print(f"  [{c.get('priorite','?').upper():7}] {c.get('nom','?')} "
          f"({c.get('intention','?')}) — {len(reqs)} req.")
    for q in reqs[:4]:
        print(f"      - {q}")
    b = c.get("brief", {})
    if b:
        print(f"      -> {b.get('titre','')}")
    print()
if result.get("sources_recommandees"):
    print("Pour aller plus loin (acces a brancher) :")
    for s in result["sources_recommandees"]:
        print("   +", s)
print("\nSauvegarde : seo_result.json")
