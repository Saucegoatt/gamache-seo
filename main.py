"""
Gamache - Intentions SEO (Cloud Run) — GROUNDING + anti-hallucination.

L'autocompletion Suggest est bloquee depuis l'IP Cloud Run -> on s'ancre sur
Google Search via Gemini (cote serveurs Google, AUCUN blocage IP), tout en gardant
l'anti-hallucination :
  1) extraction des services REELS du site (lecture, pas d'invention) ;
  2) ancrage Gemini sur Google Search (vraies recherches, locales) ;
  3) structuration JSON avec filtre STRICT (ecarte tout service non offert).
GET /  GET /health  POST /analyze  POST /refine. Auth Vertex : SA attachee (aucune cle).
"""
import os
import re
import json
import time
import urllib.request
from urllib.parse import urlparse, urlencode

import google.auth
import google.auth.transport.requests
import requests
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__)
app.json.ensure_ascii = False

HERE = os.path.dirname(__file__)
PNUM = os.environ.get("GCP_PROJECT_NUMBER", "43644610505")
VREGION = os.environ.get("VERTEX_REGION", "us-central1")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-pro")
GURL = (f"https://{VREGION}-aiplatform.googleapis.com/v1/projects/{PNUM}"
        f"/locations/{VREGION}/publishers/google/models/{MODEL}:generateContent")
SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")
SERP_URL = "https://serpapi.com/search.json"
TRENDS_GEO = os.environ.get("TRENDS_GEO", "CA")
SA_EMAIL = os.environ.get("SERVICE_ACCOUNT_EMAIL", "meetsync-sa@notebooklm-entreprise-498216.iam.gserviceaccount.com")
IMPERSONATE = os.environ.get("IMPERSONATE_SUBJECT", "yannis@gamachemedia.com")
ADS_DEV_TOKEN = os.environ.get("ADS_DEV_TOKEN", "")
ADS_LOGIN_CID = os.environ.get("ADS_LOGIN_CUSTOMER_ID", "7802443211")
ADS_CID = os.environ.get("ADS_CUSTOMER_ID", "7802443211")
ADS_VERSION = os.environ.get("ADS_API_VERSION", "v20")
ADS_GEO = os.environ.get("ADS_GEO", "2124")
ADS_LANG = os.environ.get("ADS_LANG", "1002")

_creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])


def _bearer():
    _creds.refresh(google.auth.transport.requests.Request())
    return _creds.token


def _post(body):
    r = requests.post(GURL, headers={"Authorization": f"Bearer {_bearer()}",
                                     "Content-Type": "application/json"},
                      json=body, timeout=240)
    r.raise_for_status()
    return r.json()


def _text(data):
    try:
        return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
    except Exception:
        return ""


def _parse(txt):
    txt = re.sub(r"^```(json)?\s*", "", (txt or "").strip())
    txt = re.sub(r"\s*```$", "", txt).strip()
    try:
        return json.loads(txt)
    except Exception:
        m = re.search(r"\{.*\}", txt, re.S)
        return json.loads(m.group(0)) if m else {}


def fetch_site(url):
    p = urlparse(url if url.startswith("http") else "https://" + url)
    root = f"{p.scheme}://{p.netloc}/"
    try:
        req = urllib.request.Request(root, headers={"User-Agent": "Mozilla/5.0 GamacheSEO"})
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read(400000).decode("utf-8", "replace")
    except Exception:
        return "", p.netloc
    html = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", html)).strip()[:5000], p.netloc


def extract_seeds(context, client):
    prompt = (
        "A partir du contenu fourni, liste les services PRINCIPAUX REELLEMENT offerts par "
        "le client (sa section 'nos services' ou le contenu descriptif), 6 a 8 max, en "
        "mots-cles courts (francais du Quebec). IGNORE les listes d'options de formulaires "
        "de contact ou de soumission (gabarits generiques) : un terme qui apparait "
        "UNIQUEMENT dans un formulaire ne doit PAS etre liste. "
        f"Client : {client}\nContenu : {context}\n"
        "Reponds UNIQUEMENT en JSON : {\"seeds\":[\"...\"]}"
    )
    j = _parse(_text(_post({"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1024,
                                                 "responseMimeType": "application/json"}})))
    s = [x.strip() for x in (j.get("seeds") or []) if isinstance(x, str) and x.strip()]
    return s[:8] or [client]


def ground(client, region, context, services):
    slist = ", ".join(services)
    prompt = (
        "Tu es un strategiste SEO senior au Quebec. En t'appuyant sur de VRAIES recherches "
        "Google (sers-toi de l'outil de recherche), identifie les intentions de recherche "
        "pertinentes pour ce client dans SA region.\n\n"
        f"Client : {client}\nRegion : {region}\n"
        f"Services REELLEMENT offerts (extraits du site) : {slist}\n"
        f"Contexte (site) : {context}\n\n"
        "Donne les requetes reelles que les gens tapent (People Also Ask, variations locales) "
        "et ce qui ressort des resultats Google. REGLE STRICTE : reste UNIQUEMENT dans les "
        "services offerts ci-dessus ; n'aborde AUCUN service que le client n'offre pas. "
        "Fort accent LOCAL."
    )
    data = _post({"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                  "tools": [{"googleSearch": {}}],
                  "generationConfig": {"temperature": 0.3, "maxOutputTokens": 8192}})
    gm = (data.get("candidates", [{}])[0].get("groundingMetadata") or {})
    return _text(data), gm.get("webSearchQueries", [])


SCHEMA = ('{"client":"...","region":"...","note_donnees":"<1 phrase>",'
          '"sources_recommandees":["Search Console (requetes reelles + positions)",'
          '"GA4 (conversions par page)","Token Google Ads (volumes de recherche)"],'
          '"clusters":[{"nom":"...","intention":"informationnel|commercial|transactionnel|local",'
          '"priorite":"haute|moyenne|basse","terme_trend":"<terme court 1-3 mots pour Google Trends>","requetes":["..."],'
          '"brief":{"titre":"...","angle":"...","faq":["...","...","..."]}}]}')


def structure(client, region, raw, services):
    slist = ", ".join(services)
    prompt = (
        "Structure l'analyse SEO suivante en JSON exploitable.\n\n"
        f"Client : {client}\nRegion : {region}\n"
        f"Services REELLEMENT offerts : {slist}\n\nAnalyse :\n{raw}\n\n"
        "REGLE ABSOLUE : ECARTE tout cluster ou requete portant sur un service ABSENT de la "
        "liste des services offerts (si le client ne fait pas ce service, ne le garde pas), "
        "le hors-sujet et les noms de concurrents. Le champ note_donnees precise que l'analyse "
        "s'appuie sur le site + de vraies recherches Google (ancrage), sans Search Console ni "
        "Analytics. Pour chaque cluster, terme_trend = un terme COURT (1-3 mots, SANS ville) propice "
        "a Google Trends (ex. 'mini excavation', 'pave uni'). "
        "Donne 6 a 9 clusters. Reponds UNIQUEMENT en JSON : " + SCHEMA
    )
    return _parse(_text(_post({"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                               "generationConfig": {"temperature": 0.3, "maxOutputTokens": 8192,
                                                    "responseMimeType": "application/json"}})))


def refine_result(prev, instruction, region):
    prompt = (
        "Voici une analyse SEO en JSON. Applique la demande et renvoie le MEME format JSON "
        "complet (garde 6 a 9 clusters, conserve note_donnees et sources_recommandees). "
        "N'invente pas de faux services.\n\n"
        f"Region : {region}\nDemande : {instruction}\n\n"
        f"JSON actuel :\n{json.dumps(prev, ensure_ascii=False)[:7000]}\n\n"
        "Reponds UNIQUEMENT le JSON complet mis a jour : " + SCHEMA
    )
    return _parse(_text(_post({"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                               "generationConfig": {"temperature": 0.4, "maxOutputTokens": 8192,
                                                    "responseMimeType": "application/json"}})))


def trends(terms):
    """Google Trends 12 mois (via SerpApi) pour <=5 termes. {} si pas de cle/erreur."""
    if not SERPAPI_KEY or not terms:
        return {}
    params = {"engine": "google_trends", "q": ",".join(terms[:5]),
              "data_type": "TIMESERIES", "date": "today 12-m", "geo": TRENDS_GEO,
              "hl": "fr", "api_key": SERPAPI_KEY}
    try:
        req = urllib.request.Request(SERP_URL + "?" + urlencode(params),
                                     headers={"User-Agent": "GamacheSEO"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return {}
    series = {}
    for pt in (data.get("interest_over_time", {}).get("timeline_data") or []):
        for v in (pt.get("values") or []):
            q, val = v.get("query", ""), v.get("extracted_value")
            if q and isinstance(val, (int, float)):
                series.setdefault(q, []).append((pt.get("date", ""), val))
    out = {}
    for q, pts in series.items():
        vals = [v for _, v in pts]
        if len(vals) < 2:
            continue
        n = max(2, len(vals) // 4)
        old, new = sum(vals[:n]) / n, sum(vals[-n:]) / n
        var = round((new - old) / old * 100) if old > 0 else 0
        direction = "en hausse" if var >= 15 else ("en baisse" if var <= -15 else "stable")
        peak = max(pts, key=lambda x: x[1])[0] if pts else ""
        out[q] = {"direction": direction, "variation_pct": var, "pic": peak,
                  "interet_moyen": round(sum(vals) / len(vals))}
    return out


def _ads_token():
    now = int(time.time())
    claims = {"iss": SA_EMAIL, "sub": IMPERSONATE,
              "scope": "https://www.googleapis.com/auth/adwords",
              "aud": "https://oauth2.googleapis.com/token", "iat": now, "exp": now + 3600}
    sign = requests.post(
        f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{SA_EMAIL}:signJwt",
        headers={"Authorization": f"Bearer {_bearer()}"},
        json={"payload": json.dumps(claims)}, timeout=20)
    sign.raise_for_status()
    tok = requests.post("https://oauth2.googleapis.com/token", timeout=20, data={
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": sign.json()["signedJwt"]})
    tok.raise_for_status()
    return tok.json()["access_token"]


def keyword_volumes(keywords):
    """{mot_cle_minuscule: volume mensuel moyen} via Keyword Planner. {} si pas de token/erreur."""
    kws = list(dict.fromkeys(k.strip() for k in keywords if k and k.strip()))[:20]
    if not ADS_DEV_TOKEN or not kws:
        return {}
    url = (f"https://googleads.googleapis.com/{ADS_VERSION}/customers/{ADS_CID}"
           ":generateKeywordHistoricalMetrics")
    body = {"keywords": kws,
            "geoTargetConstants": [f"geoTargetConstants/{ADS_GEO}"],
            "language": f"languageConstants/{ADS_LANG}",
            "keywordPlanNetwork": "GOOGLE_SEARCH"}
    try:
        r = requests.post(url, headers={"Authorization": f"Bearer {_ads_token()}",
                                        "developer-token": ADS_DEV_TOKEN,
                                        "login-customer-id": ADS_LOGIN_CID,
                                        "Content-Type": "application/json"},
                          json=body, timeout=40)
        r.raise_for_status()
        data = r.json()
    except requests.HTTPError as e:
        body = e.response.text if e.response is not None else ""
        print("keyword_volumes HTTP", getattr(e.response, "status_code", "?"), body[:700])
        return {}
    except Exception as exc:
        print("keyword_volumes: erreur ->", str(exc)[:200])
        return {}
    out = {}
    for res in (data.get("results") or []):
        t = (res.get("text") or "").strip().lower()
        vol = (res.get("keywordMetrics") or {}).get("avgMonthlySearches")
        if t and vol is not None:
            try:
                out[t] = int(vol)
            except Exception:
                pass
    return out


@app.get("/")
def index():
    return send_from_directory(HERE, "index.html")


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "gamache-seo", "model": MODEL})


@app.post("/analyze")
def analyze():
    d = request.get_json(silent=True) or {}
    seed = (d.get("seed") or "").strip()
    region = (d.get("region") or "").strip() or "Quebec"
    if not seed:
        return jsonify({"error": "seed manquant"}), 400
    if seed.startswith("http"):
        ctx, client = fetch_site(seed)
        ctx = ctx or seed
    else:
        ctx, client = seed, seed.split(",")[0].strip()
    try:
        services = extract_seeds(ctx, client)
        raw, searches = ground(client, region, ctx, services)
        result = structure(client, region, raw, services)
    except requests.HTTPError as e:
        detail = e.response.text if e.response is not None else str(e)
        return jsonify({"error": "gemini", "detail": detail[:400]}), 502
    result.setdefault("client", client)
    result.setdefault("region", region)
    result["recherches_google"] = searches[:18]
    result["nb_requetes_reelles"] = len(searches)
    result["services"] = services
    clusters = result.get("clusters", [])
    terms = [t for t in ((c.get("terme_trend") or c.get("nom") or "").strip() for c in clusters) if t]
    tr = {}
    for i in range(0, min(len(terms), 10), 5):
        tr.update(trends(terms[i:i + 5]))
    for c in clusters:
        key = (c.get("terme_trend") or c.get("nom") or "").strip()
        if key in tr:
            c["tendance"] = tr[key]
    volmap = keyword_volumes(terms)
    for c in clusters:
        vkey = (c.get("terme_trend") or c.get("nom") or "").strip().lower()
        if vkey in volmap:
            c["volume"] = volmap[vkey]
    return jsonify(result)


@app.post("/refine")
def refine():
    d = request.get_json(silent=True) or {}
    instruction = (d.get("instruction") or "").strip()
    if not instruction:
        return jsonify({"error": "instruction manquante"}), 400
    prev = d.get("result") or {}
    region = (d.get("region") or prev.get("region") or "Quebec").strip()
    try:
        updated = refine_result(prev, instruction, region)
    except requests.HTTPError as e:
        detail = e.response.text if e.response is not None else str(e)
        return jsonify({"error": "gemini", "detail": detail[:400]}), 502
    updated.setdefault("recherches_google", prev.get("recherches_google", []))
    return jsonify(updated)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
