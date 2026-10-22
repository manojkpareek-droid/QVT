"""QVT Scorecard - Quality / Value / Trend scoring for NSE/BSE stocks.
Run locally:  streamlit run app.py
Educational scoring tool. Not investment advice.
"""
import streamlit as st
import yfinance as yf
import pandas as pd

st.set_page_config(page_title="QVT Scorecard", page_icon="📊", layout="centered")

# ---------------------------------------------------------------
# 1. RULES - edit thresholds here to express YOUR methodology.
# Each rule: (label, function that pulls value, test, display format)
# ---------------------------------------------------------------
def pct(x): return f"{x*100:.1f}%"
def num(x): return f"{x:.2f}"

def build_rules(i, hist, is_financial):
    price = i.get("currentPrice") or i.get("regularMarketPrice")
    ocf, ni = i.get("operatingCashflow"), i.get("netIncomeToCommon")
    cash_conv = (ocf / ni) if (ocf is not None and ni and ni > 0) else None
    d200 = i.get("twoHundredDayAverage")
    ret6m = None
    if hist is not None and len(hist) > 120:
        ret6m = hist["Close"].iloc[-1] / hist["Close"].iloc[-126] - 1
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
        ("Price above 200-day average", (price / d200 - 1) if price and d200 else None, lambda v: v > 0, pct),
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

@st.cache_data(ttl=3600, show_spinner=False)
def load(symbol):
    t = yf.Ticker(symbol)
    return t.info, t.history(period="1y")

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
        with st.spinner("Fetching data..."):
            info, hist = load(ticker)
        if not info or not (info.get("currentPrice") or info.get("regularMarketPrice")):
            st.error("No data found. Check the symbol (BSE needs the code or name used by Yahoo).")
            st.stop()
    except Exception as e:
        st.error(f"Data fetch failed: {e}")
        st.stop()

    is_fin = info.get("sector") == "Financial Services"
    st.subheader(info.get("longName", symbol))
    st.write(f"{info.get('sector','')} · {info.get('industry','')}")

    results = {k: score_pillar(v) for k, v in build_rules(info, hist, is_fin).items()}
    valid = {k: s for k, (s, _) in results.items() if s is not None}
    if valid:
        w = sum(WEIGHTS[k] for k in valid)
        total = round(sum(valid[k] * WEIGHTS[k] for k in valid) / w)
    else:
        total = None

    if total is not None:
        band = "Strong" if total >= 70 else "Moderate" if total >= 50 else "Weak"
        st.metric("QVT Score", f"{total} / 100", band + " QVT profile")
    cols = st.columns(3)
    for c, k in zip(cols, results):
        s = results[k][0]
        c.metric(k, "n/a" if s is None else f"{s}/100")

    for k, (s, rows) in results.items():
        with st.expander(f"{k} details"):
            st.dataframe(pd.DataFrame(rows, columns=["Test", "Value", "Result"]),
                         hide_index=True, use_container_width=True)

    st.caption("Data: Yahoo Finance (unofficial, may be delayed or incomplete — verify with "
               "exchange filings). Score is a mechanical screen, not a recommendation to buy, "
               "sell or hold. Not investment advice.")
