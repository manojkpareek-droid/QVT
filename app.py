"""QVT Scorecard - Quality / Value / Trend scoring for NSE/BSE stocks.
Run locally:  streamlit run app.py
Educational scoring tool. Not investment advice.

Data sources
  Prices        : TejHQ (primary, needs TEJ_API_KEY in secrets) -> Yahoo (fallback)
  Fundamentals  : Yahoo Finance (optional; missing fundamentals show as n/a)
"""
import os
from datetime import date, timedelta

import pandas as pd
import streamlit as st
import yfinance as yf

try:
    from tej import Client as TejClient   # pip install tejhq
except ImportError:
    TejClient = None

st.set_page_config(page_title="QVT Scorecard", page_icon="📊", layout="centered")


# ---------------------------------------------------------------
# 0. DATA LAYER
# ---------------------------------------------------------------
def get_tej_key():
    """API key comes from Streamlit secrets (or env var). Never hardcode it."""
    try:
        key = st.secrets["TEJ_API_KEY"]
    except Exception:
        key = os.environ.get("TEJ_API_KEY")
    return key or None


def rows_to_hist(rows, col):
    """TejHQ rows (list of dicts) -> DataFrame with a 'Close' column, date-indexed."""
    df = pd.DataFrame(rows)
    if df.empty or col not in df.columns or "date" not in df.columns:
        return None
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").set_index("date")
    out = pd.DataFrame({"Close": pd.to_numeric(df[col], errors="coerce")}).dropna()
    return out if not out.empty else None


def tej_history(symbol, exch):
    """Returns (hist, source_label, notes). Raises if TejHQ cannot supply prices."""
    if TejClient is None:
        raise RuntimeError("'tejhq' package not installed (add it to requirements.txt)")
    key = get_tej_key()
    client = TejClient(api_key=key, timeout=20.0, max_retries=2)
    ex = exch.lower()
    start = (date.today() - timedelta(days=400)).isoformat()  # ~270 trading days
    end = date.today().isoformat()
    notes = []
    if key:  # back-adjusted prices (free-key endpoint) are best for MA / returns
        try:
            hist = rows_to_hist(client.adjusted(symbol, ex, start, end), "adj_close")
            if hist is not None:
                return hist, "TejHQ (split/bonus-adjusted)", notes
            notes.append("TejHQ adjusted series was empty; using unadjusted prices.")
        except Exception as e:
            notes.append(f"TejHQ adjusted endpoint failed ({type(e).__name__}: {str(e)[:100]}); "
                         "using unadjusted prices.")
    hist = rows_to_hist(client.ohlcv(symbol, ex, start, end), "close")
    if hist is None:
        raise RuntimeError(f"TejHQ has no {exch} price rows for '{symbol}' "
                           "(wrong symbol, SME/other series, or not listed in the covered period).")
    return hist, "TejHQ (unadjusted)", notes


@st.cache_data(ttl=3600, show_spinner=False)
def load_prices(symbol, exch):
    """TejHQ first, Yahoo second. Raises if both fail, so failures are NOT cached."""
    notes = []
    try:
        return tej_history(symbol, exch)
    except Exception as e:
        notes.append(f"TejHQ failed: {type(e).__name__}: {str(e)[:150]}. Fell back to Yahoo prices.")
    ticker = symbol + (".NS" if exch == "NSE" else ".BO")
    hist = yf.Ticker(ticker).history(period="1y")
    if hist is None or hist.empty:
        raise RuntimeError("No price data from TejHQ or Yahoo. " + " ".join(notes))
    hist = hist[["Close"]].dropna()
    hist.index = hist.index.tz_localize(None)
    return hist, "Yahoo Finance (fallback)", notes


@st.cache_data(ttl=3600, show_spinner=False)
def load_fundamentals(ticker):
    """Yahoo fundamentals. Raises on failure so failures are NOT cached."""
    info = yf.Ticker(ticker).info
    if not info or len(info) < 5:
        raise RuntimeError("Yahoo returned no fundamentals")
    return info


# ---------------------------------------------------------------
# 1. RULES - edit thresholds here to express YOUR methodology.
# Each rule: (label, value, test, display format). value=None -> n/a (not a fail).
# ---------------------------------------------------------------
def pct(x): return f"{x*100:.1f}%"
def num(x): return f"{x:.2f}"

def build_rules(i, hist, is_financial):
    closes = hist["Close"]
    price = float(closes.iloc[-1])
    d200 = float(closes.tail(200).mean()) if len(closes) >= 200 else None
    ret6m = float(closes.iloc[-1] / closes.iloc[-127] - 1) if len(closes) > 126 else None

    ocf, ni = i.get("operatingCashflow"), i.get("netIncomeToCommon")
    cash_conv = (ocf / ni) if (ocf is not None and ni and ni > 0) else None
    de = i.get("debtToEquity")
    de = de / 100 if de is not None else None  # yfinance gives percent

    quality = [
        ("ROE > 15%", i.get("returnOnEquity"), lambda v: v > 0.15, pct),
        ("Net profit margin > 10%", i.get("profitMargins"), lambda v: v > 0.10, pct),
        ("Cash conversion (OCF/NI) > 0.8", cash_conv, lambda v: v > 0.8, num),
        ("Insider/promoter holding > 40%", i.get("heldPercentInsiders"), lambda v: v > 0.40, pct),
    ]
    if not is_financial:  # D/E is meaningless for banks/NBFCs
        quality.insert(1, ("Debt/Equity < 0.5", de, lambda v: v < 0.5, num))

    value = [
        ("P/E < 25", i.get("trailingPE"), lambda v: 0 < v < 25, num),
        ("P/B < 4", i.get("priceToBook"), lambda v: 0 < v < 4, num),
        ("PEG < 1.5", i.get("pegRatio") or i.get("trailingPegRatio"), lambda v: 0 < v < 1.5, num),
    ]
    if not is_financial:
        value.append(("EV/EBITDA < 15", i.get("enterpriseToEbitda"), lambda v: 0 < v < 15, num))

    trend = [
        ("Earnings growth > 15%", i.get("earningsGrowth"), lambda v: v > 0.15, pct),
        ("Revenue growth > 10%", i.get("revenueGrowth"), lambda v: v > 0.10, pct),
        ("Price above 200-day average", (price / d200 - 1) if d200 else None, lambda v: v > 0, pct),
        ("6-month price return > 0", ret6m, lambda v: v > 0, pct),
    ]
    return {"Quality": quality, "Value": value, "Trend": trend}

WEIGHTS = {"Quality": 0.4, "Value": 0.3, "Trend": 0.3}

def score_pillar(rules):
    rows, passed, avail = [], 0, 0
    for label, val, test, fmt in rules:
        if val is None:
            rows.append((label, "n/a", "—"))
            continue
        ok = bool(test(val))
        avail += 1
        passed += ok
        rows.append((label, fmt(val), "✅" if ok else "❌"))
    score = round(passed / avail * 100) if avail >= 2 else None
    return score, rows


# ---------------------------------------------------------------
# 2. UI
# ---------------------------------------------------------------
st.title("📊 QVT Scorecard")
st.caption("Quality · Value · Trend — a rules-based screen for NSE/BSE stocks")

symbol = st.text_input("NSE symbol (e.g. EMUDHRA, TCS, RELIANCE)").strip().upper()
exch = st.radio("Exchange", ["NSE", "BSE"], horizontal=True)

if symbol:
    ticker = symbol + (".NS" if exch == "NSE" else ".BO")

    try:
        with st.spinner("Fetching prices..."):
            hist, price_src, notes = load_prices(symbol, exch)
    except Exception as e:
        st.error(f"Could not fetch prices for {symbol} on {exch}.")
        st.code(f"{type(e).__name__}: {e}")
        st.stop()
    for n in notes:
        st.warning(n)

    info, fund_err = {}, None
    try:
        with st.spinner("Fetching fundamentals..."):
            info = load_fundamentals(ticker)
    except Exception as e:
        fund_err = f"{type(e).__name__}: {str(e)[:150]}"
        st.warning("Fundamentals unavailable from Yahoo right now, so Quality, Value and growth "
                   "tests show n/a (not failed). Only price-based Trend tests could be scored.")

    with st.expander("Debug"):
        st.write(f"Ticker: {ticker} | prices: {price_src}, {len(hist)} rows, "
                 f"last {hist.index[-1].date()} | fundamentals fields: {len(info)}"
                 + (f" | error: {fund_err}" if fund_err else ""))

    is_fin = info.get("sector") == "Financial Services"
    st.subheader(info.get("longName", symbol))
    if info:
        st.write(f"{info.get('sector','')} · {info.get('industry','')}")

    results = {k: score_pillar(v) for k, v in build_rules(info, hist, is_fin).items()}
    valid = {k: s for k, (s, _) in results.items() if s is not None}
    if valid:
        w = sum(WEIGHTS[k] for k in valid)
        total = round(sum(valid[k] * WEIGHTS[k] for k in valid) / w)
    else:
        total = None

    all_rows = [r for _, rows in results.values() for r in rows]
    done = sum(1 for r in all_rows if r[2] != "—")

    if total is not None and len(valid) >= 2:
        band = "Strong" if total >= 70 else "Moderate" if total >= 50 else "Weak"
        st.metric("QVT Score", f"{total} / 100", band + " QVT profile")
        st.caption(f"Based on {done} of {len(all_rows)} tests with data "
                   f"(pillars scored: {', '.join(valid)}).")
        if len(valid) < 3:
            st.info("Partial score: the missing pillar is excluded and the remaining weights "
                    "are rescaled, so treat this number with caution.")
    else:
        st.metric("QVT Score", "n/a")
        st.info(f"Only {done} of {len(all_rows)} tests had data (pillars scored: "
                f"{', '.join(valid) or 'none'}). A headline score needs at least two pillars, "
                "so none is shown. See the pillar details below.")
    cols = st.columns(3)
    for c, k in zip(cols, results):
        s = results[k][0]
        c.metric(k, "n/a" if s is None else f"{s}/100")

    for k, (s, rows) in results.items():
        with st.expander(f"{k} details"):
            st.dataframe(pd.DataFrame(rows, columns=["Test", "Value", "Result"]),
                         hide_index=True, use_container_width=True)

    st.caption(f"Prices: {price_src}. Fundamentals: Yahoo Finance (unofficial, may be delayed or "
               "incomplete — verify with exchange filings). Score is a mechanical screen, not a "
               "recommendation to buy, sell or hold. Not investment advice.")
