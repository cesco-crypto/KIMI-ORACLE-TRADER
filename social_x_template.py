#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SOCIAL_X v1 — X(Twitter)-Schicht der Social-Engine. Nur Python-Stdlib, keine Installation.
Laeuft auf dem MacBook/Mac mini: alle CADENCE_MIN Minuten:
  1) Counts-Velocity pro Coin (1 Call gibt 168 Stunden-Buckets = Baseline GRATIS dabei)
  2) Whale-Poller (Top-N Timeline-Reads) -> Coin-Match -> ALERT
  3) Budget-Waechter (Stop bei Tagesbudget in USD)
Ausgabe: social_x_heatmap.json + Log auf stdout
Start:  python3 social_x.py
Config: unten im CONFIG-Block anpassen
"""
import json, os, time, sys, math
import urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone

# ================= CONFIG =================
BEARER = "DEIN_X_BEARER_TOKEN"
OUT_DIR = os.path.expanduser("~/social_engine")          # Ausgabe-Ordner auf deinem Mac
COINS = [  # (query, symbol) — CASHTAG-Strategie: Mini-Caps rein $TICKER, Majors $TICKER OR Name
    ("$PUMP OR pump.fun", "PUMPUSDT"), ("$KAITO", "KAITOUSDT"), ("$ESP", "ESPUSDT"),
    ("$WLD OR worldcoin", "WLDUSDT"), ("$INJ OR injective", "INJUSDT"), ("$SOL OR solana", "SOLUSDT"),
    ("$ETH OR ethereum", "ETHUSDT"), ("$DOGE OR dogecoin", "DOGEUSDT"), ("$PEPE", "1000PEPEUSDT"),
    ("$HYPE OR hyperliquid", "HYPEUSDT"), ("$SUI", "SUIUSDT"), ("$XRP OR ripple", "XRPUSDT"),
    ("$ADA OR cardano", "ADAUSDT"), ("$LINK OR chainlink", "LINKUSDT"), ("$AVAX OR avalanche", "AVAXUSDT"),
    ("$NEAR", "NEARUSDT"), ("$ARB OR arbitrum", "ARBUSDT"), ("$ONDO", "ONDOUSDT"),
    ("$TRX OR tron", "TRXUSDT"), ("$WIF", "WIFUSDT"), ("$FARTCOIN", "FARTCOINUSDT"),
    ("$VIRTUAL", "VIRTUALUSDT"), ("$AAVE", "AAVEUSDT"), ("$UNI OR uniswap", "UNIUSDT"),
    ("$CAKE OR pancakeswap", "CAKEUSDT"), ("$SNX", "SNXUSDT"), ("$LTC OR litecoin", "LTCUSDT"),
    ("$BCH", "BCHUSDT"), ("$TAO OR bittensor", "TAOUSDT"), ("$ENA OR ethena", "ENAUSDT"),
    ("$ON", "ONUSDT"), ("$TAG", "TAGUSDT"), ("$BEAT", "BEATUSDT"), ("$LA", "LAUSDT"),
    ("$BTW", "BTWUSDT"), ("$BANK", "BANKUSDT"), ("$AKE", "AKEUSDT"), ("$EUL", "EULUSDT"),
]
WHALES = [  # Top-Whales v1 (username) — IDs werden automatisch aufgeloest
    "elonmusk", "cz_binance", "lookonchain", "whale_alert", "WatcherGuru",
    "cobie", "Kaleo", "Pentosh1", "MustStopMurad", "zachxbt",
    "gem_detecter", "MilesDeutscher", "binance", "BinanceFutures", "kucoincom",
]
CADENCE_MIN = 60          # Scan-Intervall Minuten (Counts)
WHALE_POLL_MIN = 60       # Whale-Timeline-Intervall Minuten
DAILY_BUDGET_USD = 3.0    # harte Tagesbremse (100 $ = ~1 Monat Reserve)
COST_PER_READ = 0.005     # konservativ geschaetzt (Pay-per-Use)
ALERT_Z = 2.5             # z-Score Schwelle fuer HYPE-Flag

# --- GITHUB-BRUECKE (pusht Heatmap ins Repo, Kimi-Engine liest dort) ---
GH_TOKEN = "DEIN_GITHUB_TOKEN"
GH_OWNER = "cesco-crypto"
GH_REPO = "KIMI-ORACLE-TRADER"
GH_PATH = "data/social_x_heatmap.json"
GH_PUSH_EVERY_MIN = 30    # Push-Intervall Minuten
# ===========================================

API = "https://api.x.com/2"
UA = {"Authorization": "Bearer " + BEARER, "User-Agent": "social_x/1.0"}
state = {"reads_today": 0, "cost_today": 0.0, "day": None, "seen_posts": set(),
         "whale_ids": {}, "baselines": {}, "budget_hit": False}


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


def api_get(path, params=None):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            state["reads_today"] += 1
            state["cost_today"] = round(state["cost_today"] + COST_PER_READ, 4)
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:200]
        log(f"HTTP {e.code}: {body}")
        if e.code == 429:
            log("Rate-Limit — pausiere 5 Min"); time.sleep(300)
        return None
    except Exception as e:
        log("ERR:", type(e).__name__, str(e)[:120])
        return None


def budget_ok():
    today = datetime.now(timezone.utc).date().isoformat()
    if state["day"] != today:
        state.update(day=today, reads_today=0, cost_today=0.0, budget_hit=False)
        log(f"— Neuer Tag {today}: Budget zurueckgesetzt —")
    if state["cost_today"] >= DAILY_BUDGET_USD:
        if not state["budget_hit"]:
            state["budget_hit"] = True
            log(f"⚠ TAGESBUDGET ${DAILY_BUDGET_USD} erreicht — pausiere bis Mitternacht")
        return False
    return True


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def mad(xs):
    m = median(xs)
    return median([abs(x - m) for x in xs]) or 1.0


def coin_counts(query):
    """1 Call = letzte 7 Tage stuendliche Mentions-Counts. Baseline gratis."""
    d = api_get("/tweets/counts/recent", {"query": query, "granularity": "hour"})
    if not d or "data" not in d:
        return None
    pts = [(p["start"], p["tweet_count"]) for p in d["data"]]
    pts.sort()
    return pts


def analyze(symbol, pts):
    """Velocity-z des letzten vollen Stunden-Buckets vs. 7d-Stunden-Baseline (MAD)."""
    if len(pts) < 30:
        return None
    counts = [c for _, c in pts]
    cur = counts[-2] if len(counts) > 1 else counts[-1]  # letzte volle Stunde
    base = counts[:-2]
    med, m = median(base), mad(base)
    z = (cur - med) / (1.4826 * m)
    # Acceleration: letzte 3h vs. vorherige 3h
    a = (sum(counts[-4:-1]) - sum(counts[-7:-4])) / 3.0 if len(counts) > 7 else 0
    zc = max(-8, min(8, z))
    cat = ("HYPE" if zc >= 4 else "RISING" if zc >= ALERT_Z else
           "COOLING" if zc <= -1.5 else "normal")
    return dict(symbol=symbol, mentions_last_h=cur, baseline_h=round(med),
                z=round(zc, 2), accel_3h=round(a), category=cat)


def resolve_whales():
    for u in WHALES:
        if u in state["whale_ids"]:
            continue
        d = api_get("/users/by/username/" + u)
        if d and "data" in d:
            state["whale_ids"][u] = d["data"]["id"]
        time.sleep(0.4)
    log(f"Whales aufgeloest: {len(state['whale_ids'])}/{len(WHALES)}")


def whale_poll():
    if not state["whale_ids"]:
        resolve_whales()
    alerts = []
    for u, uid in state["whale_ids"].items():
        d = api_get(f"/users/{uid}/tweets",
                    {"max_results": 5, "tweet.fields": "created_at,text"})
        time.sleep(0.5)
        if not d or "data" not in d:
            continue
        for p in d["data"]:
            pid = p["id"]
            if pid in state["seen_posts"]:
                continue
            state["seen_posts"].add(pid)
            txt = p["text"].lower()
            hits = [sym for q, sym in COINS if any(k in txt for k in q.lower().split()[:1])]
            alerts.append(dict(whale=u, text=p["text"][:160],
                               created=p["created_at"], coins=hits))
    return alerts


def write_heatmap(coin_stats, whale_alerts):
    os.makedirs(OUT_DIR, exist_ok=True)
    out = dict(ts=int(time.time() * 1000),
               budget=dict(cost_today=state["cost_today"], limit=DAILY_BUDGET_USD,
                           reads_today=state["reads_today"]),
               coins=sorted([c for c in coin_stats if c],
                            key=lambda x: -x["z"]),
               whale_alerts=whale_alerts)
    with open(os.path.join(OUT_DIR, "social_x_heatmap.json"), "w") as f:
        json.dump(out, f, indent=1)
    maybe_push_github(out)


def maybe_push_github(payload):
    """Pusht die Heatmap ins GitHub-Repo (Contents-API, kein git noetig)."""
    if not GH_TOKEN:
        return
    now = time.time()
    if now - state.get("last_gh_push", 0) < GH_PUSH_EVERY_MIN * 60:
        return
    state["last_gh_push"] = now
    try:
        import base64
        content = base64.b64encode(json.dumps(payload, indent=1).encode()).decode()
        url = f"https://api.github.com/repos/{GH_OWNER}/{GH_REPO}/contents/{GH_PATH}"
        headers = {"Authorization": "token " + GH_TOKEN,
                   "Accept": "application/vnd.github+json", "User-Agent": "social_x/1.0"}
        sha = None
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                sha = json.loads(r.read().decode())["sha"]
        except Exception:
            pass
        body = {"message": f"social_x heatmap {datetime.now(timezone.utc).isoformat(timespec='minutes')}",
                "content": content}
        if sha:
            body["sha"] = sha
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, headers={**headers, "Content-Type": "application/json"}, method="PUT")
        with urllib.request.urlopen(req, timeout=25) as r:
            log(f"→ GitHub gepusht ({GH_PATH})")
    except Exception as e:
        log("GH-PUSH-ERR:", type(e).__name__, str(e)[:120])


ANOMALY_URL = "https://raw.githubusercontent.com/cesco-crypto/KIMI-ORACLE-TRADER/main/data/binance_anomalies.json"

def fetch_anomalies():
    """Liest die Anomalie-Liste der Engine (Binance-Entdecker) aus dem Repo."""
    try:
        req = urllib.request.Request(ANOMALY_URL, headers={"User-Agent": "social_x/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode()).get("anomalies", [])
    except Exception as e:
        log("ANOMALIE-FETCH-ERR:", type(e).__name__)
        return []


def discovery_cycle():
    """KASKADE: Binance entdeckt -> X bestaetigt. Queried X nur fuer Anomalie-Coins."""
    anos = fetch_anomalies()
    if not anos:
        return []
    if not budget_ok():
        return []
    log(f"DISCOVERY: {len(anos)} Anomalien von der Engine — X-Validierung laeuft")
    results = []
    for a in anos[:10]:  # Top-10 Anomalien reichen
        base, sym = a["base"], a["symbol"]
        pts = coin_counts(f"${base}")   # CASHTAG-Suche (Praezision fuer Mini-Caps)
        if pts:
            an = analyze(sym, pts)
            if an:
                an.update(chg24=a["chg24"], z_price=a["z"], direction=a["dir"],
                          fund=a["fund"], hype_confirmed=an["z"] >= 2.0,
                          pump_without_hype=an["z"] < 1.0)
                if an["pump_without_hype"]:
                    an["category"] = "BOT_SUSPECT"
                results.append(an)
                log(f"  {sym}: Preis z{a['z']:+.1f} | X z{an['z']:+.1f} | {an['category']}"
                    + (" ⚠PUMP-OHNE-HYPE" if an["pump_without_hype"] else ""))
        time.sleep(0.8)
    return results


def cycle():
    if not budget_ok():
        return
    stats, alerts = [], []
    discovery = discovery_cycle()
    stats.extend(discovery)
    for q, sym in COINS:
        if any(s["symbol"] == sym for s in stats):
            continue  # schon per Discovery geprueft
        pts = coin_counts(q)
        if pts:
            a = analyze(sym, pts)
            if a:
                stats.append(a)
        time.sleep(0.8)
    log(f"Counts: {len(stats)} Coins | Budget: ${state['cost_today']:.2f}")
    if state["reads_today"] % 2 == 0:  # Whale-Poll jeden 2. Counts-Run
        wa = whale_poll()
        alerts.extend(wa)
        for w in wa:
            log(f"⚡ WHALE: @{w['whale']} -> {w['coins']} | {w['text'][:80]}")
    write_heatmap(stats, alerts)
    top = [c for c in stats if c["z"] >= ALERT_Z]
    for c in top:
        log(f"🔥 {c['category']:7s} {c['symbol']:14s} z={c['z']:+.1f} ({c['mentions_last_h']}/h vs Basis {c['baseline_h']}/h)")


def main():
    log("=== SOCIAL_X v1 gestartet ===")
    log(f"Coins: {len(COINS)} | Whales: {len(WHALES)} | Budget: ${DAILY_BUDGET_USD}/Tag | Intervall: {CADENCE_MIN}min")
    while True:
        try:
            cycle()
        except Exception as e:
            log("CYCLE-ERR:", type(e).__name__, str(e)[:150])
        time.sleep(CADENCE_MIN * 60)


if __name__ == "__main__":
    main()
