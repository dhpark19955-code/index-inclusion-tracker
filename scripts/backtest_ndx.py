"""Nasdaq-100 백테스트: 과거 기준일의 시총 순위로 선정 로직을 돌려 실제 공식 변경과 대조.

과거 시총 = 현재 시총 × (기준일 종가 / 최근 종가), yfinance 수정주가. 주식수 변동은 무시(근사).
멤버십은 현재 구성에서 공식 변경 로그를 역순으로 되감아 재구성.

  python backtest_ndx.py
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from common import CACHE, snap_dir, read_csv, load_json, save_json, log
from fetch_us_market import canon
import screen_us

# 공식 변경 로그 (회사 단위, 시간순) — 출처: ir.nasdaq.com 공지·S&P/언론 (2026-10-12 조사)
CHANGES = [
    ("2025-12-22", ["ALNY", "FER", "INSM", "MPWR", "STX", "WDC"], ["BIIB", "CDW", "GFS", "LULU", "ON", "TTD"]),
    ("2026-01-20", ["WMT"], ["AZN"]),
    ("2026-04-20", ["SNDK"], ["TEAM"]),
    ("2026-05-18", ["LITE"], ["CSGP"]),
    ("2026-06-22", ["ALAB", "CRWV", "NBIS", "RKLB", "TER"], ["CHTR", "CTSH", "INSM", "VRSK", "ZS"]),
    ("2026-06-29", ["HONA"], []),
    ("2026-07-07", ["SPCX"], []),
    ("2026-08-05", [], ["EA"]),
    ("2026-09-14", [], ["KHC"]),
    ("2026-10-09", ["MRNA"], ["WBD"]),
]
# 상장폐지/이전 종목 현재가·주식수 근사 (yfinance 가격 이력은 남아 있음)
GONE_SHARES = {"EA": 251e6, "WBD": 2.47e9, "AZN": 3.1e9}

BT = [  # 라벨, 기준일, 유형(recon|quarterly), 실제 편입, 실제 편출
    ("2025-12 연례 재구성", "2025-11-28", "recon",
     {"ALNY", "FER", "INSM", "MPWR", "STX", "WDC"}, {"BIIB", "CDW", "GFS", "LULU", "ON", "TTD"}),
    ("2026-06 분기변경", "2026-05-29", "quarterly",
     {"ALAB", "CRWV", "NBIS", "RKLB", "TER"}, {"CHTR", "CTSH", "INSM", "VRSK", "ZS"}),
]


def members_at(date: str, current: set[str]) -> set[str]:
    m = set(current)
    for d, ins, outs in reversed(CHANGES):
        if d > date:
            m -= set(ins)
            m |= set(outs)
    return m


def _px_ratio(tickers: list[str], ref: str) -> dict[str, float]:
    """ref일 종가 / 최근 종가 (수정주가). 캐시."""
    import yfinance as yf
    cache = CACHE / f"ndx_bt_px_{ref}.json"
    got = load_json(cache, default={}) or {}
    todo = [t for t in tickers if t not in got]
    for i in range(0, len(todo), 60):
        chunk = todo[i:i + 60]
        df = yf.download(chunk, start=ref, end=dt.date.today().isoformat(),
                         progress=False, auto_adjust=True)["Close"]
        if isinstance(df, pd.Series):
            df = df.to_frame(chunk[0])
        ref_ts = df.index[df.index >= ref][0]
        for t in chunk:
            try:
                s = df[t].dropna()
                got[t] = float(s.loc[ref_ts] / s.iloc[-1]) if len(s) and ref_ts in s.index else None
            except (KeyError, IndexError):
                got[t] = None
    save_json(got, cache)
    return got


def build_frame(ref: str, members: set[str]) -> pd.DataFrame:
    """기준일 시총 추정 프레임 (회사 단위, NDX 적격 필터 적용)."""
    sd = snap_dir()
    u = read_csv(sd / "us_universe.csv")
    ndx_now = read_csv(sd / "const_ndx.csv")
    r = screen_us.screen_ndx(u, ndx_now)   # 적격성·회사 합산·ADR 분리 재사용
    t = r["table"] if False else None
    # screen_ndx 는 상위만 돌려주므로 전체 적격 테이블을 다시 구성
    x = screen_us._prep(u[u["exchange"].eq("NASDAQ")])
    reit = x["industry"].fillna("").str.contains("Real Estate Investment Trust", case=False)
    x = x[x["sec_ok"] & ~x["sector"].isin(screen_us.PARAMS["ndx_excl_sectors"]) & ~reit & x["mcap"].notna()]
    comp = x.groupby("ckey", sort=False).agg(symbol=("symbol", "first"), name=("name", "first"),
                                             mcap=("mcap", "max"), ipoyear=("ipoyear", "max")).reset_index()
    comp["symbol"] = comp["symbol"].map(canon)
    # ADR(비미국 본국상장) 제외 — 시뮬과 동일
    adr = comp["name"].str.contains("American Depositary|ADS|New York Registry", na=False)
    us_only = ~adr | comp["symbol"].isin(members)
    comp = comp[us_only]
    # 사라진 과거 멤버 보충
    extra = [{"ckey": s.lower(), "symbol": s, "name": s + " (상장폐지/이전)", "mcap": None, "ipoyear": None}
             for s in members if s not in set(comp["symbol"])]
    comp = pd.concat([comp, pd.DataFrame(extra)], ignore_index=True)
    ratios = _px_ratio(sorted(set(comp.nlargest(220, "mcap")["symbol"]) | members), ref)
    import yfinance as yf
    rows = []
    for rec in comp.itertuples(index=False):
        s = rec.symbol
        if s in ratios and ratios[s] and rec.mcap:
            mc = rec.mcap * ratios[s]
        elif s in GONE_SHARES:
            try:
                h = yf.download(s, start=ref, end=(dt.date.fromisoformat(ref) + dt.timedelta(days=7)).isoformat(),
                                progress=False, auto_adjust=True)["Close"].dropna()
                mc = float(h.iloc[0]) * GONE_SHARES[s]
            except Exception:
                mc = None
        elif rec.mcap and s not in ratios:
            continue  # 가격 미조회 소형주 — 순위권 밖
        else:
            mc = None
        if mc:
            # 기준일 이후 상장(가격 이력 없음) 제외
            rows.append({"symbol": s, "name": rec.name, "mcap_then": mc, "ipoyear": rec.ipoyear})
    f = pd.DataFrame(rows).sort_values("mcap_then", ascending=False).reset_index(drop=True)
    f["member"] = f["symbol"].isin(members)
    f["rank"] = range(1, len(f) + 1)
    return f


def run():
    sd = snap_dir()
    cur_members = set()
    ndx_now = read_csv(sd / "const_ndx.csv")
    u = read_csv(sd / "us_universe.csv")
    x = screen_us._prep(u[u["exchange"].eq("NASDAQ")])
    sym2key = dict(zip(x["symbol"].map(canon), x["ckey"]))
    # 회사 대표심볼 집합 (클래스 통합: GOOG/GOOGL → 1)
    seen = {}
    for s in ndx_now["symbol"].map(canon):
        k = sym2key.get(s, s.lower())
        seen.setdefault(k, s)
    cur_members = set(seen.values())

    out = []
    for label, ref, kind, act_in, act_out in BT:
        members = members_at(ref, cur_members)
        f = build_frame(ref, members)
        f = f[~(f["ipoyear"].fillna(0) >= int(ref[:4])) | f["member"]]  # 기준일 이후/당해 IPO 비멤버 제외(시즈닝 근사)
        f = f.reset_index(drop=True)
        f["rank"] = range(1, len(f) + 1)
        sim = f.rename(columns={"symbol": "ckey2"})
        if kind == "recon":
            chosen = screen_us._simulate_recon(
                f.assign(ckey=f["symbol"]), n=100, auto=75, buf=125)
            p_in = {s for s in chosen if s not in members}
            p_out = {s for s in members if s not in chosen and s in set(f["symbol"])}
        else:  # quarterly: 125위 밖 멤버 제거 → 100 미만이면 최대 비멤버로 충원
            mem_f = f[f["member"]]
            drops = set(mem_f[mem_f["rank"] > 125]["symbol"])
            n_after = len(members) - len(drops)
            fills = f[~f["member"] & (f["rank"] <= 100)].head(max(0, 100 - n_after))
            p_in, p_out = set(fills["symbol"]), drops
        nm = dict(zip(f["symbol"], f["name"]))
        rk = dict(zip(f["symbol"], f["rank"]))
        fmt = lambda ss: ", ".join(f"{s}({rk.get(s, '?')}위)" for s in sorted(ss))
        out.append({"review": label, "ref": ref, "예측 편입": fmt(p_in), "예측 편출": fmt(p_out),
                    "실제 편입": fmt(act_in), "실제 편출": fmt(act_out),
                    "편입 적중": f"{len(p_in & act_in)}/{len(act_in)}", "편출 적중": f"{len(p_out & act_out)}/{len(act_out)}",
                    "오탐": fmt((p_in - act_in) | (p_out - act_out))})
        f.head(150).to_csv(sd / f"bt_ndx_{ref}.csv", index=False, encoding="utf-8-sig")
        log(f"[bt-ndx] {label}: 편입 {len(p_in & act_in)}/{len(act_in)} 편출 {len(p_out & act_out)}/{len(act_out)}")
    return pd.DataFrame(out)


if __name__ == "__main__":
    pd.set_option("display.width", 250)
    pd.set_option("display.max_colwidth", 400)
    print(run().T.to_string())
