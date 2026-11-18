"""미국 전 종목 시총 스냅샷(nasdaq.com screener) + SEC GAAP 순이익(S&P 500 흑자 요건용)."""
from __future__ import annotations

import datetime as dt

import pandas as pd

from common import CACHE, SEC_UA, get, num, today, snap_dir, load_json, save_json, log

SEC = CACHE / "sec"
SEC.mkdir(parents=True, exist_ok=True)


def canon(sym: str) -> str:
    return str(sym).strip().upper().replace(".", "-").replace("/", "-").replace(" ", "")


def universe() -> pd.DataFrame:
    frames = []
    for ex in ("NASDAQ", "NYSE", "AMEX"):
        r = get("https://api.nasdaq.com/api/screener/stocks",
                params={"tableonly": "true", "limit": "10000", "exchange": ex, "download": "true"},
                headers={"Accept": "application/json"}, timeout=60)
        df = pd.DataFrame(r.json()["data"]["rows"])
        df["exchange"] = ex
        frames.append(df)
    u = pd.concat(frames, ignore_index=True)
    out = pd.DataFrame({
        "symbol": u["symbol"].map(canon), "name": u["name"], "exchange": u["exchange"],
        "price": u["lastsale"].map(num), "mcap": u["marketCap"].map(num), "volume": u["volume"].map(num),
        "country": u["country"], "ipoyear": u["ipoyear"].map(num), "sector": u["sector"], "industry": u["industry"],
    })
    out = out.sort_values("mcap", ascending=False, na_position="last").reset_index(drop=True)
    return out


# ---------- SEC: 분기 GAAP 순이익 ----------
_TICKER_CIK: dict[str, int] | None = None


def cik_of(sym: str) -> int | None:
    global _TICKER_CIK
    if _TICKER_CIK is None:
        path = SEC / "company_tickers.json"
        j = load_json(path)
        if not j or (dt.datetime.now().timestamp() - path.stat().st_mtime) > 7 * 86400:
            j = get("https://www.sec.gov/files/company_tickers.json", headers={"User-Agent": SEC_UA}).json()
            save_json(j, path)
        _TICKER_CIK = {canon(v["ticker"]): int(v["cik_str"]) for v in j.values()}
    return _TICKER_CIK.get(canon(sym))


NI_TAGS = ["NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic", "ProfitLoss",
           "IncomeLossFromContinuingOperations"]


def _concept(cik: int, tag: str, taxonomy: str = "us-gaap") -> list[dict]:
    path = SEC / (f"{cik}_{tag}.json" if taxonomy == "us-gaap" else f"{cik}_{taxonomy}_{tag}.json")
    j = load_json(path)
    fresh = j is not None and (dt.datetime.now().timestamp() - path.stat().st_mtime) < 3 * 86400
    if not fresh:
        try:
            j = get(f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/{taxonomy}/{tag}.json",
                    headers={"User-Agent": SEC_UA}, tries=2).json()
        except Exception:
            j = {"units": {}}
        save_json(j, path)
    return (j.get("units") or {}).get("USD", [])


def public_float(sym: str) -> dict:
    """10-K 표지 공시 유동시총(dei:EntityPublicFloat). 기준일이 직전 회계연도 2분기 말이라 최대 1년 지연.
    일부 종목은 태깅 오류가 있어 '참고'용 — 미달이어도 탈락이 아니라 확인 표시만 한다."""
    cik = cik_of(sym)
    if not cik:
        return {}
    facts = [f for f in _concept(cik, "EntityPublicFloat", "dei") if f.get("val")]
    if not facts:
        return {}
    last = max(facts, key=lambda x: (x.get("end", ""), x.get("filed", "")))
    return {"float_usd": float(last["val"]), "float_asof": last.get("end", "")}


def px_ratio(sym: str, ref: str) -> float | None:
    """ref일 종가 / 최근 종가 (yfinance 수정주가, 캐시). 과거 시총 스케일링용."""
    import yfinance as yf
    cache = SEC.parent / "px_ratio.json"
    got = load_json(cache, default={}) or {}
    key = f"{sym}@{ref}"
    if key not in got:
        try:
            s = yf.download(sym, start=ref, end=dt.date.today().isoformat(),
                            progress=False, auto_adjust=True)["Close"].dropna()
            got[key] = float(s.iloc[0].item() / s.iloc[-1].item()) if len(s) > 1 else None
        except Exception:
            got[key] = None
        save_json(got, cache)
    return got[key]


def gaap_quarters(sym: str, as_of: str | None = None) -> dict:
    """최근 4개 분기 GAAP 순이익. 10-Q 분기값 + 10-K 연간에서 Q4 역산.

    as_of='YYYY-MM-DD' 를 주면 그 날짜까지 제출된 공시만으로 판정(백테스트용).
    반환: {'q': [(end, val)...최근순], 'latest_q': float, 'ttm': float, 'tag': str} 또는 {'err': ...}
    """
    cik = cik_of(sym)
    if not cik:
        return {"err": "CIK 없음"}
    for tag in NI_TAGS:
        facts = [f for f in _concept(cik, tag) if f.get("form") in ("10-Q", "10-K", "10-Q/A", "10-K/A")
                 and f.get("start") and f.get("end")
                 and (as_of is None or f.get("filed", "9999") <= as_of)]
        if not facts:
            continue
        # 같은 기간이면 가장 최근 filed
        best: dict[tuple, dict] = {}
        for f in facts:
            k = (f["start"], f["end"])
            if k not in best or f["filed"] > best[k]["filed"]:
                best[k] = f
        q, annual = {}, {}
        for (s, e), f in best.items():
            days = (dt.date.fromisoformat(e) - dt.date.fromisoformat(s)).days
            if 80 <= days <= 100:
                q[e] = f["val"]
            elif 350 <= days <= 380:
                annual[e] = (s, f["val"])
        # Q4 = 연간 − 같은 회계연도 3개 분기
        for e, (s, val) in annual.items():
            if e in q:
                continue
            inside = [v for qe, v in q.items() if s < qe < e]
            if len(inside) == 3:
                q[e] = val - sum(inside)
        ends = sorted(q, reverse=True)[:4]
        if len(ends) < 4:
            continue
        # 분기 연속성 확인 (각 간격 60~120일)
        ds = [dt.date.fromisoformat(x) for x in ends]
        if any(not 60 <= (ds[i] - ds[i + 1]).days <= 120 for i in range(3)):
            continue
        vals = [q[e] for e in ends]
        return {"q": list(zip(ends, vals)), "latest_q": vals[0], "ttm": sum(vals), "tag": tag,
                "latest_end": ends[0]}
    return {"err": "분기 순이익 4개 미확보"}


def us_returns(symbols: list[str]) -> pd.DataFrame:
    """미국 종목 1주/1개월/3개월 수익률 (yfinance 일봉, 일 단위 캐시)."""
    import yfinance as yf
    cache = CACHE / f"us_returns_{dt.date.today():%Y%m%d}.json"
    got = load_json(cache, default={}) or {}
    todo = sorted({canon(s) for s in symbols} - set(got))
    for i in range(0, len(todo), 60):
        chunk = todo[i:i + 60]
        try:
            px = yf.download(chunk, period="5mo", progress=False, auto_adjust=True)["Close"]
        except Exception:
            continue
        if isinstance(px, pd.Series):
            px = px.to_frame(chunk[0])
        for sym in chunk:
            try:
                ser = px[sym].dropna()
            except KeyError:
                got[sym] = {}
                continue
            if len(ser) < 2:
                got[sym] = {}
                continue
            last_dt, last = ser.index[-1], float(ser.iloc[-1])
            rec = {}
            for d in (7, 30, 91):
                w = ser[ser.index <= last_dt - pd.Timedelta(days=d)]
                rec[f"ret_{d}d"] = last / float(w.iloc[-1]) - 1 if len(w) else None
            got[sym] = rec
    save_json(got, cache)
    return pd.DataFrame.from_dict(got, orient="index").rename_axis("_sym").reset_index()


def run() -> pd.DataFrame:
    u = universe()
    u.to_csv(snap_dir() / "us_universe.csv", index=False, encoding="utf-8-sig")
    log(f"[us] universe {len(u)}종목 (NASDAQ {sum(u.exchange == 'NASDAQ')}, NYSE {sum(u.exchange == 'NYSE')})")
    return u


if __name__ == "__main__":
    run()
