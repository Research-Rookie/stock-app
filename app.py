import os, sys
# Ensure templates can be found from both local and Vercel (api/) contexts
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if _BASE_DIR not in sys.path:
    sys.path.insert(0, _BASE_DIR)
import json, time, threading, functools, re
from datetime import datetime
import requests
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS

app = Flask(__name__, template_folder=os.path.join(_BASE_DIR, 'templates'))
CORS(app)

# ═══════════════════════════════════════════════════════
# Yahoo Finance session with cookie + crumb auth
# ═══════════════════════════════════════════════════════

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
})

_crumb = None
_crumb_lock = threading.Lock()
_crumb_ts = 0

def _get_crumb():
    """Get Yahoo Finance crumb (auto-refresh every 5 min)."""
    global _crumb, _crumb_ts
    with _crumb_lock:
        if _crumb and time.monotonic() - _crumb_ts < 300:
            return _crumb
        try:
            # Step 1: get cookie from fc.yahoo.com
            session.get("https://fc.yahoo.com/", timeout=10)
            # Step 2: get crumb
            r = session.get("https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=10)
            _crumb = r.text.strip()
            _crumb_ts = time.monotonic()
            return _crumb
        except Exception:
            return None

# ── Rate limiter ──
_last_req = 0
_rate_lock = threading.Lock()

def rate_wait():
    global _last_req
    with _rate_lock:
        e = time.monotonic() - _last_req
        if e < 1.2:
            time.sleep(1.2 - e)
        _last_req = time.monotonic()

# ── Cache ──
_cache = {}
_cache_lock = threading.Lock()
TTL = 60

def cget(k):
    with _cache_lock:
        e = _cache.get(k)
        if e and time.monotonic() - e["t"] < TTL:
            return e["d"]
    return None

def cset(k, d):
    with _cache_lock:
        _cache[k] = {"d": d, "t": time.monotonic()}

# ── HTTP helper ──
def yf_get(url, params=None, retries=2):
    crumb = _get_crumb()
    if crumb:
        params = dict(params or {})
        params["crumb"] = crumb

    last_err = None
    for attempt in range(retries + 1):
        try:
            rate_wait()
            r = session.get(url, params=params, timeout=10)
            if r.status_code == 401:
                # crumb expired, force refresh
                global _crumb
                with _crumb_lock:
                    _crumb = None
                crumb = _get_crumb()
                if crumb and params is not None:
                    params["crumb"] = crumb
                rate_wait()
                r = session.get(url, params=params, timeout=10)
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


# ═══════════════════════════════════════════════════════
# Routes
# ═══════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/quote")
def quote():
    symbol = request.args.get("symbol", "").strip().upper()
    if not symbol:
        return jsonify({"error": "请输入股票代码"}), 400

    ck = f"q:{symbol}"
    cached = cget(ck)
    if cached:
        return jsonify(cached)

    try:
        data = yf_get("https://query2.finance.yahoo.com/v7/finance/quote",
                       params={"symbols": symbol})
        results = data.get("quoteResponse", {}).get("result", [])
        if not results:
            return jsonify({"error": f"找不到股票 {symbol}，请检查代码是否正确"}), 404

        q = results[0]
        price = q.get("regularMarketPrice") or q.get("regularMarketPreviousClose") or 0
        prev  = q.get("regularMarketPreviousClose") or 0
        chg   = round(price - prev, 2) if price and prev else 0
        chgp  = round(chg / prev * 100, 2) if prev else 0

        out = {
            "symbol": q.get("symbol", symbol),
            "name": q.get("shortName") or q.get("longName", symbol),
            "price": price,
            "change": chg,
            "changePercent": chgp,
            "previousClose": prev,
            "open": q.get("regularMarketOpen"),
            "high": q.get("regularMarketDayHigh"),
            "low": q.get("regularMarketDayLow"),
            "volume": q.get("regularMarketVolume"),
            "marketCap": q.get("marketCap"),
            "currency": q.get("currency", "USD"),
            "exchange": q.get("fullExchangeName", ""),
            "updated": datetime.now().isoformat(),
        }
        cset(ck, out)
        return jsonify(out)

    except Exception as e:
        return jsonify({"error": f"请求失败: {e}"}), 500


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
        "1d": ("1d", "5m"), "5d": ("5d", "15m"),
        "1mo": ("1mo", "1h"), "3mo": ("3mo", "1d"),
        "6mo": ("6mo", "1d"), "1y": ("1y", "1d"), "5y": ("5y", "1wk"),
    }
    range_, interval = mapping.get(period, ("1d", "5m"))

    try:
        data = yf_get(f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}",
                       params={"range": range_, "interval": interval, "includePrePost": "false"})
        result = data.get("chart", {}).get("result", [])
        if not result:
            return jsonify([])

        r = result[0]
        ts  = r.get("timestamp", [])
        ohlc = r.get("indicators", {}).get("quote", [{}])[0]
        opens  = ohlc.get("open", [])
        highs  = ohlc.get("high", [])
        lows   = ohlc.get("low", [])
        closes = ohlc.get("close", [])
        vols   = ohlc.get("volume", [])

        out = []
        for i, t in enumerate(ts):
            out.append({
                "time": datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M"),
                "open":  round(opens[i], 2)  if i < len(opens)  and opens[i]  is not None else None,
                "high":  round(highs[i], 2)  if i < len(highs)  and highs[i]  is not None else None,
                "low":   round(lows[i], 2)   if i < len(lows)   and lows[i]   is not None else None,
                "close": round(closes[i], 2) if i < len(closes) and closes[i] is not None else None,
                "volume": int(vols[i]) if i < len(vols) and vols[i] is not None else 0,
            })
        cset(ck, out)
        return jsonify(out)

    except Exception as e:
        return jsonify({"error": f"获取历史数据失败: {e}"}), 500


@app.route("/api/search")
def search():
    q = request.args.get("q", "").strip()
    if len(q) < 1:
        return jsonify([])
    ck = f"s:{q}"
    cached = cget(ck)
    if cached:
        return jsonify(cached)
    try:
        data = yf_get("https://query2.finance.yahoo.com/v1/finance/search",
                       params={"q": q, "quotesCount": 6})
        quotes = data.get("quotes", [])
        out = []
        for item in quotes:
            if item.get("quoteType") in ("EQUITY", "ETF"):
                out.append({
                    "symbol": item.get("symbol"),
                    "name": item.get("shortname") or item.get("longname", ""),
                    "exchange": item.get("exchange", ""),
                })
        cset(ck, out)
        return jsonify(out)
    except Exception:
        return jsonify([])



# ═══════════════════════════════════════════════════════
# News Aggregator Routes
# ═══════════════════════════════════════════════════════

@app.route("/news")
def news_page():
    return render_template("news.html")


@app.route("/api/news")
def get_news():
    """Fetch news from Yahoo Finance RSS + sentiment analysis."""
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    import feedparser

    symbol = request.args.get("symbol", "").strip().upper()
    if not symbol:
        return jsonify({"error": "Symbol required"}), 400

    ck = f"news:{symbol}"
    cached = cget(ck)
    if cached:
        return jsonify(cached)

    try:
        # Try Yahoo Finance RSS feed (free, no auth)
        rss_url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US"
        feed = feedparser.parse(rss_url)

        if feed.bozo and not feed.entries:
            # Fallback: try the search-based news approach
            return jsonify({"articles": [], "sentiment": {"label": "neutral", "score": 0, "positive": 0, "negative": 0, "neutral": 0}})

        analyzer = SentimentIntensityAnalyzer()
        articles = []
        pos = neg = neu = 0

        for entry in feed.entries[:15]:
            title = entry.get("title", "").strip()
            summary = entry.get("summary", "").strip()
            # Clean HTML from summary
            import re as _re
            summary = _re.sub(r"<[^>]+>", "", summary)[:300]
            published = entry.get("published", "")
            link = entry.get("link", "")

            # Sentiment on title + summary
            text = f"{title} {summary}"
            scores = analyzer.polarity_scores(text)
            compound = scores["compound"]

            if compound >= 0.05:
                sentiment = "positive"
                pos += 1
            elif compound <= -0.05:
                sentiment = "negative"
                neg += 1
            else:
                sentiment = "neutral"
                neu += 1

            articles.append({
                "title": title,
                "summary": summary,
                "url": link,
                "published": published,
                "sentiment": sentiment,
                "score": round(compound, 2),
            })

        total = pos + neg + neu
        overall = "positive" if pos > neg else ("negative" if neg > pos else "neutral")
        overall_score = round((pos - neg) / max(total, 1), 2)

        result = {
            "symbol": symbol,
            "articles": articles,
            "sentiment": {
                "label": overall,
                "score": overall_score,
                "positive": pos,
                "negative": neg,
                "neutral": neu,
                "total": total,
            },
        }
        cset(ck, result)
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/news/search")
def news_search():
    """Search for stock symbols (reuse existing search)."""
    q = request.args.get("q", "").strip()
    if len(q) < 1:
        return jsonify([])
    ck = f"ns:{q}"
    cached = cget(ck)
    if cached:
        return jsonify(cached)
    try:
        data = yf_get("https://query2.finance.yahoo.com/v1/finance/search",
                       params={"q": q, "quotesCount": 8})
        quotes = data.get("quotes", [])
        out = []
        for item in quotes:
            if item.get("quoteType") in ("EQUITY", "ETF"):
                out.append({
                    "symbol": item.get("symbol"),
                    "name": item.get("shortname") or item.get("longname", ""),
                    "exchange": item.get("exchange", ""),
                })
        cset(ck, out)
        return jsonify(out)
    except Exception:
        return jsonify([])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
