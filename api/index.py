# Vercel Serverless — Financial Stock & News API
# Single file, no threading, no global session, Vercel-compatible.

import json, time, re, os
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from io import BytesIO
import sys

import requests
from flask import Flask, request, jsonify, render_template, redirect

# ── Flask app with correct template path ──
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)  # api/ -> project root
TEMPLATE_DIR = os.path.join(ROOT_DIR, "templates")

app = Flask(__name__, template_folder=TEMPLATE_DIR)

# ── Simple cache (no locks — Vercel is single-threaded per invocation) ──
_cache = {}
CACHE_TTL = 60

def cget(k):
    e = _cache.get(k)
    if e and time.monotonic() - e["t"] < CACHE_TTL:
        return e["d"]
    return None

def cset(k, d):
    _cache[k] = {"d": d, "t": time.monotonic()}

# ── Rate limiter (per-invocation, simple) ──
_last_req = 0
def rate_wait():
    global _last_req
    now = time.monotonic()
    gap = now - _last_req
    if gap < 1.2:
        time.sleep(1.2 - gap)
    _last_req = time.monotonic()

# ── Yahoo Finance cookie+crumb (per cold-start) ──
_yf_session = None
_yf_crumb = None
_yf_crumb_ts = 0

def _get_yf():
    global _yf_session, _yf_crumb, _yf_crumb_ts
    if _yf_session is None:
        _yf_session = requests.Session()
        _yf_session.headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        )
    now = time.monotonic()
    if _yf_crumb is None or now - _yf_crumb_ts > 240:
        try:
            _yf_session.get("https://fc.yahoo.com/", timeout=10)
            r = _yf_session.get("https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=10)
            _yf_crumb = r.text.strip()
            _yf_crumb_ts = now
        except Exception:
            _yf_crumb = None
    return _yf_session, _yf_crumb


def yf_get(url, params=None, retries=2):
    sess, crumb = _get_yf()
    if crumb:
        params = dict(params or {})
        params["crumb"] = crumb
    last_err = None
    for attempt in range(retries + 1):
        try:
            rate_wait()
            r = sess.get(url, params=params, timeout=10)
            if r.status_code == 401:
                # Force crumb refresh
                global _yf_crumb
                _yf_crumb = None
                sess2, crumb2 = _get_yf()
                if crumb2 and params is not None:
                    params["crumb"] = crumb2
                rate_wait()
                r = sess2.get(url, params=params, timeout=10)
            if r.status_code == 429:
                time.sleep((attempt + 1) * 3)
                continue
            if r.status_code != 200:
                last_err = Exception(f"HTTP {r.status_code}")
                continue
            return r.json()
        except Exception as e:
            last_err = e
            time.sleep((attempt + 1) * 2)
    raise last_err or Exception("Request failed")


# ═══════════════════════════════════════
# Routes
# ═══════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/news")
def news_page():
    return render_template("news.html")

@app.route("/api/quote")
def quote():
    symbol = request.args.get("symbol", "").strip().upper()
    if not symbol:
        return jsonify({"error": "Symbol required"}), 400
    ck = f"q:{symbol}"
    cached = cget(ck)
    if cached:
        return jsonify(cached)
    try:
        data = yf_get("https://query2.finance.yahoo.com/v7/finance/quote",
                       params={"symbols": symbol})
        results = data.get("quoteResponse", {}).get("result", [])
        if not results:
            return jsonify({"error": f"Symbol '{symbol}' not found."}), 404
        q = results[0]
        price = q.get("regularMarketPrice") or q.get("regularMarketPreviousClose") or 0
        prev  = q.get("regularMarketPreviousClose") or 0
        chg   = round(price - prev, 2) if price and prev else 0
        chgp  = round(chg / prev * 100, 2) if prev else 0
        out = {
            "symbol": q.get("symbol", symbol),
            "name": q.get("shortName") or q.get("longName", symbol),
            "price": price, "change": chg, "changePercent": chgp,
            "previousClose": prev, "open": q.get("regularMarketOpen"),
            "high": q.get("regularMarketDayHigh"), "low": q.get("regularMarketDayLow"),
            "volume": q.get("regularMarketVolume"), "marketCap": q.get("marketCap"),
            "currency": q.get("currency", "USD"),
            "exchange": q.get("fullExchangeName", ""),
            "updated": datetime.now().isoformat(),
        }
        cset(ck, out)
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": f"Request failed: {e}"}), 500

@app.route("/api/history")
def history():
    symbol = request.args.get("symbol", "").strip().upper()
    period = request.args.get("period", "1d")
    if not symbol:
        return jsonify({"error": "Symbol required"}), 400
    ck = f"h:{symbol}:{period}"
    cached = cget(ck)
    if cached:
        return jsonify(cached)
    mapping = {
        "1d": ("1d","5m"),"5d": ("5d","15m"),"1mo": ("1mo","1h"),
        "3mo": ("3mo","1d"),"6mo": ("6mo","1d"),"1y": ("1y","1d"),"5y": ("5y","1wk"),
    }
    range_, interval = mapping.get(period, ("1d","5m"))
    try:
        data = yf_get(f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}",
                       params={"range": range_, "interval": interval, "includePrePost": "false"})
        result = data.get("chart",{}).get("result",[])
        if not result: return jsonify([])
        r = result[0]
        ts = r.get("timestamp",[])
        ohlc = r.get("indicators",{}).get("quote",[{}])[0]
        opens,highs,lows,closes,vols = ohlc.get("open",[]),ohlc.get("high",[]),ohlc.get("low",[]),ohlc.get("close",[]),ohlc.get("volume",[])
        out = []
        for i,t in enumerate(ts):
            out.append({
                "time": datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M"),
                "open": round(opens[i],2) if i<len(opens) and opens[i] is not None else None,
                "high": round(highs[i],2) if i<len(highs) and highs[i] is not None else None,
                "low": round(lows[i],2) if i<len(lows) and lows[i] is not None else None,
                "close": round(closes[i],2) if i<len(closes) and closes[i] is not None else None,
                "volume": int(vols[i]) if i<len(vols) and vols[i] is not None else 0,
            })
        cset(ck, out)
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": f"History failed: {e}"}), 500

@app.route("/api/search")
def search():
    q = request.args.get("q","").strip()
    if len(q)<1: return jsonify([])
    ck = f"s:{q}"
    cached = cget(ck)
    if cached: return jsonify(cached)
    try:
        data = yf_get("https://query2.finance.yahoo.com/v1/finance/search",
                       params={"q":q,"quotesCount":6})
        quotes = data.get("quotes",[])
        out = [{"symbol":it.get("symbol"),"name":it.get("shortname") or it.get("longname",""),
                "exchange":it.get("exchange","")}
               for it in quotes if it.get("quoteType") in ("EQUITY","ETF")]
        cset(ck, out)
        return jsonify(out)
    except Exception:
        return jsonify([])

@app.route("/api/news")
def get_news():
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    import feedparser
    symbol = request.args.get("symbol","").strip().upper()
    if not symbol: return jsonify({"error":"Symbol required"}),400
    ck = f"news:{symbol}"
    cached = cget(ck)
    if cached: return jsonify(cached)
    try:
        feed = feedparser.parse(f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US")
        if not feed.entries:
            return jsonify({"articles":[],"sentiment":{"label":"neutral","score":0,"positive":0,"negative":0,"neutral":0}})
        analyzer = SentimentIntensityAnalyzer()
        articles=[]; pos=neg=neu=0
        for entry in feed.entries[:15]:
            title = entry.get("title","").strip()
            summary = re.sub(r"<[^>]+>","",entry.get("summary",""))[:300]
            scores = analyzer.polarity_scores(f"{title} {summary}")
            compound = scores["compound"]
            if compound>=0.05: sentiment="positive"; pos+=1
            elif compound<=-0.05: sentiment="negative"; neg+=1
            else: sentiment="neutral"; neu+=1
            articles.append({"title":title,"summary":summary,"url":entry.get("link",""),
                             "published":entry.get("published",""),"sentiment":sentiment,"score":round(compound,2)})
        total = max(pos+neg+neu,1)
        result = {"symbol":symbol,"articles":articles,
                  "sentiment":{"label":"positive" if pos>neg else ("negative" if neg>pos else "neutral"),
                               "score":round((pos-neg)/total,2),"positive":pos,"negative":neg,"neutral":neu,"total":total}}
        cset(ck, result)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error":str(e)}),500

@app.route("/api/news/search")
def news_search():
    q = request.args.get("q","").strip()
    if len(q)<1: return jsonify([])
    ck = f"ns:{q}"
    cached = cget(ck)
    if cached: return jsonify(cached)
    try:
        data = yf_get("https://query2.finance.yahoo.com/v1/finance/search",params={"q":q,"quotesCount":8})
        out = [{"symbol":it.get("symbol"),"name":it.get("shortname") or it.get("longname",""),
                "exchange":it.get("exchange","")}
               for it in data.get("quotes",[]) if it.get("quoteType") in ("EQUITY","ETF")]
        cset(ck, out)
        return jsonify(out)
    except Exception:
        return jsonify([])