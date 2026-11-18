"""S&P 500 소프트 백테스트: 2026년 실제 편입·편출(나스닥 상장분)이 발표 시점에 우리 필터에서
어떻게 판정됐을지 확인. S&P 는 위원회 재량이라 '적중률'이 아니라 '필터가 실제 선택을 걸러내지 않았는가'를 본다.

  python backtest_sp.py
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from common import CACHE, snap_dir, read_csv, load_json, save_json, log
from fetch_us_market import gaap_quarters, canon
import screen_us

# (티커, 발표일, 구분, 실제 결과, 비고)
CASES = [
    ("MRVL", "2026-06-05", "편입", "6/22 편입", ""),
    ("FLEX", "2026-06-05", "편입", "6/22 편입", ""),
    ("HONA", "2026-06-23", "편입", "6/29 편입", "하니웰 분할 신설 — 스핀오프는 시즈닝 면제"),
    ("ILMN", "2026-09-04", "편입", "9/21 편입", ""),
    ("POOL", "2026-06-05", "편출", "6/22 SmallCap 강등", ""),
    ("CPB", "2026-06-05", "편출", "6/22 SmallCap 강등", ""),
    ("TTD", "2026-09-04", "편출", "9/21 SmallCap 강등", ""),
]
MIN = screen_us.PARAMS["sp_mcap_min"]


def px_ratio(sym: str, ref: str) -> float | None:
    import yfinance as yf
    cache = CACHE / "sp_bt_px.json"
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


def run() -> pd.DataFrame:
    u = read_csv(snap_dir() / "us_universe.csv")
    u["symbol"] = u["symbol"].map(canon)
    rows = []
    for sym, ann, kind, actual, memo in CASES:
        row = {"티커": sym, "발표일": ann, "구분": kind, "실제": actual, "비고": memo}
        rec = u[u["symbol"].eq(sym)]
        ratio = px_ratio(sym, ann)
        mcap = rec["mcap"].iloc[0] * ratio if len(rec) and ratio else None
        row["발표시점 시총($B)"] = round(mcap / 1e9, 1) if mcap else None
        if kind == "편입":
            g = gaap_quarters(sym, as_of=ann)
            prof = "충족" if ("err" not in g and g["latest_q"] > 0 and g["ttm"] > 0) else \
                   g.get("err") or "미달"
            size = "충족" if mcap and mcap >= MIN else "미달"
            ipo = rec["ipoyear"].iloc[0] if len(rec) else None
            seas = "미달(상장 12개월 미만)" if ipo and ipo >= 2025.5 else "충족"
            ok = size == "충족" and prof == "충족" and seas == "충족"
            row.update({"시총요건": size, "흑자요건": prof, "시즈닝": seas,
                        "필터 통과": "O" if ok else "X"})
        else:
            row.update({"시총요건": f"하한 대비 {mcap / MIN:.2f}x" if mcap else "?",
                        "필터 통과": "편출위험군" if mcap and mcap < MIN * 0.5 else "하위권 밖"})
        rows.append(row)
        log(f"[bt-sp] {sym} {kind}: {row.get('필터 통과')}")
    return pd.DataFrame(rows)


if __name__ == "__main__":
    pd.set_option("display.width", 250)
    print(run().to_string(index=False))
