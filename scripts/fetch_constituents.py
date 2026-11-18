"""현재 지수 구성종목 스냅샷 (ETF 보유종목 프록시 + nasdaq.com).

  KOSPI200   ← KODEX 200 PDF (삼성자산운용)
  MSCI Korea ← iShares EWY 보유종목 CSV (MSCI Korea 25/50, 편입여부는 MSCI Korea와 동일)
  Nasdaq-100 ← api.nasdaq.com list-type/nasdaq100
  S&P 500    ← SPDR SPY 일별 보유종목 xlsx

종목 수가 기대 범위를 벗어나면 예외를 던진다(소스 포맷 변경 감지).
"""
from __future__ import annotations

import io
import re

import pandas as pd

from common import get, num, today, ymd, snap_dir, log

EXPECT = {"kospi200": (198, 202), "msci_korea": (60, 110), "ndx": (100, 103), "sp500": (500, 506), "sp400": (390, 410),
          "ftse_korea": (100, 200)}


def kospi200() -> pd.DataFrame:
    r = get("https://www.samsungfund.com/api/v1/kodex/product-pdf/2ETF01.do",
            params={"gijunYMD": ymd(today())})  # 휴일이면 직전 영업일 PDF가 온다
    pdf = r.json()["pdf"]
    rows = [x for x in pdf["list"] if re.fullmatch(r"[0-9A-Z]{6}", str(x.get("itmNo", "")))
            and x.get("curp")]
    df = pd.DataFrame({"code": [x["itmNo"] for x in rows],
                       "name": [x["secNm"] for x in rows],
                       "qty": [num(x["applyQ"]) for x in rows],
                       "weight": [num(x["ratio"]) for x in rows]})
    df.attrs["asof"] = pdf["gijunYMD"]
    return df


def msci_korea() -> pd.DataFrame:
    r = get("https://www.ishares.com/us/products/239681/ishares-msci-south-korea-etf/latest-holdings.csv")
    lines = r.text.splitlines()
    asof = next((l.split(",", 1)[1].strip('" ') for l in lines[:3] if l.startswith("Fund Holdings as of")), "")
    hdr = next(i for i, l in enumerate(lines) if l.startswith("Ticker,"))
    df = pd.read_csv(io.StringIO("\n".join(lines[hdr:])), dtype=str)
    df = df[df["Asset Class"].eq("Equity") & df["Ticker"].str.fullmatch(r"[0-9A-Z]{6}", na=False)]
    out = pd.DataFrame({"code": df["Ticker"], "name": df["Name"], "sector": df["Sector"],
                        "weight": df["Weight (%)"].map(num), "qty": df["Quantity"].map(num),
                        "price_usd": df["Price"].map(num), "fx": df["FX Rate"].map(num)})
    out.attrs["asof"] = asof
    return out.reset_index(drop=True)


def ndx() -> pd.DataFrame:
    r = get("https://api.nasdaq.com/api/quote/list-type/nasdaq100", headers={"Accept": "application/json"})
    rows = r.json()["data"]["data"]["rows"]
    df = pd.DataFrame({"symbol": [x["symbol"] for x in rows],
                       "name": [x["companyName"] for x in rows],
                       "mcap": [num(x["marketCap"]) for x in rows]})
    df.attrs["asof"] = ymd(today())
    return df


def sp500() -> pd.DataFrame:
    r = get("https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/etfs/us/"
            "holdings-daily-us-en-spy.xlsx")
    raw = pd.read_excel(io.BytesIO(r.content), header=None)
    asof = str(raw.iloc[2, 1]).replace("As of ", "")
    hdr = raw.index[raw.iloc[:, 0].astype(str).eq("Name")][0]
    df = raw.iloc[hdr + 1:].copy()
    df.columns = [str(c) for c in raw.iloc[hdr]]
    df = df[df["Ticker"].notna() & df["Identifier"].notna()]
    df = df[~df["Ticker"].astype(str).str.contains("CASH|USD|-", regex=True)]
    out = pd.DataFrame({"symbol": df["Ticker"].astype(str).str.replace(".", "-", regex=False),
                        "name": df["Name"], "weight": df["Weight"].map(num), "sector": df["Sector"]})
    out.attrs["asof"] = asof
    return out.reset_index(drop=True)


def sp400() -> pd.DataFrame:
    """S&P MidCap 400 (iShares IJH) — S&P 500 승격 후보군 표시용."""
    r = get("https://www.ishares.com/us/products/239763/ishares-core-sp-midcap-etf/latest-holdings.csv")
    lines = r.text.splitlines()
    asof = next((l.split(",", 1)[1].strip('" ') for l in lines[:3] if l.startswith("Fund Holdings as of")), "")
    hdr = next(i for i, l in enumerate(lines) if l.startswith("Ticker,"))
    df = pd.read_csv(io.StringIO("\n".join(lines[hdr:])), dtype=str, keep_default_na=False)
    df = df[df["Asset Class"].eq("Equity")]
    out = pd.DataFrame({"symbol": df["Ticker"].str.replace(".", "-", regex=False).str.replace(" ", "-", regex=False),
                        "name": df["Name"]})
    out.attrs["asof"] = asof
    return out.reset_index(drop=True)


def ftse_korea() -> pd.DataFrame:
    """FTSE All-World(대·중형) 한국 구성 ← Vanguard VEU 보유종목(FTSE All-World ex US, 월말 기준·지연)."""
    rows, start = [], 1
    for _ in range(30):
        j = get("https://investor.vanguard.com/vmf/api/VEU/portfolio-holding/stock.json",
                params={"start": start, "count": 500},
                headers={"Accept": "application/json", "Referer": "https://investor.vanguard.com/"}).json()
        ent = (j.get("fund") or {}).get("entity") or []
        rows += ent
        if not j.get("next") or len(ent) < 500:   # next 가 없으면 끝 (계속 넘기면 1페이지로 되돌아감)
            break
        start += 500
    kr = [e for e in rows if str(e.get("isin", "")).startswith("KR7")]
    df = pd.DataFrame({"code": [str(e["ticker"]).zfill(6) for e in kr], "name": [e["longName"] for e in kr],
                       "weight": [num(e.get("percentWeight")) for e in kr], "isin": [e["isin"] for e in kr]})
    df = df.drop_duplicates("code")
    df.attrs["asof"] = (kr[0]["asOfDate"][:10] if kr else "")
    return df.reset_index(drop=True)


FETCHERS = {"kospi200": kospi200, "msci_korea": msci_korea, "ndx": ndx, "sp500": sp500, "sp400": sp400,
            "ftse_korea": ftse_korea}


def run() -> dict[str, pd.DataFrame]:
    out, sd = {}, snap_dir()
    for key, fn in FETCHERS.items():
        try:
            df = fn()
        except Exception as e:  # 한 소스 실패가 전체를 막지 않게
            log(f"[constituents] {key} FAILED: {type(e).__name__} {e}")
            continue
        lo, hi = EXPECT[key]
        n = len(df)
        if not lo <= n <= hi:
            log(f"[constituents] {key} 종목수 {n} — 기대범위 {lo}~{hi} 벗어남, 저장 안 함")
            continue
        df.to_csv(sd / f"const_{key}.csv", index=False, encoding="utf-8-sig")
        (sd / f"const_{key}.asof").write_text(str(df.attrs.get("asof", "")), encoding="utf-8")
        log(f"[constituents] {key}: {n}종목 (asof {df.attrs.get('asof')})")
        out[key] = df
    return out


if __name__ == "__main__":
    run()
