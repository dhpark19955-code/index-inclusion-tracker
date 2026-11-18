"""MSCI Korea 모델 백테스트: 리뷰 직전 EWY 구성 + 당시 가격 판정구간 시총으로 GIMI 규칙 적용 → MSCI 공식 변경과 대조.

공식 변경(MSCI STPublicList PDF): https://app2.msci.com/eqb/gimi/stdindex/MSCI_{Mon}{YY}_STPublicList.pdf
  python backtest_msci.py
"""
from __future__ import annotations

import datetime as dt
import io

import pandas as pd

from common import get, snap_dir, read_csv, log
from kr_metrics import price_frame, float_table
import screen_kr as S
import calibrate_msci as C

# 라벨, 직전 EWY 날짜, 가격구간 시작·끝, 기준선 C(조원, 증권사 추정 역산), 공식 편입, 공식 편출
REVIEWS = [
    ("2025-11", "20251114", "2025-10-17", "2025-10-31", 6.3, {"443060", "278470"}, {"271560"}),
    ("2026-08", "20260814", "2026-07-17", "2026-07-31", 7.75, {"011070"}, {"028300", "034220", "047050", "0126Z0"}),
]


def ewy_weights(d: str) -> tuple[pd.DataFrame, float]:
    lines = get(C.EWY.format(d=d)).text.splitlines()
    h = next(i for i, l in enumerate(lines) if l.startswith("Ticker,"))
    df = pd.read_csv(io.StringIO("\n".join(lines[h:])), dtype=str)
    df = df[df["Asset Class"].eq("Equity") & ~df["Name"].str.contains("RIGHTS", na=False)]
    df["code"] = [t if isinstance(t, str) and len(t) == 6 and not t.endswith("D") else C.NAME_MAP.get(n)
                  for t, n in zip(df["Ticker"], df["Name"])]
    df["weight"] = pd.to_numeric(df["Weight (%)"], errors="coerce")
    fx = pd.to_numeric(df["FX Rate"].str.replace(",", ""), errors="coerce").median()
    return df.dropna(subset=["code"])[["code", "Name", "weight"]], float(fx)


def run() -> pd.DataFrame:
    P = S._params()
    u = read_csv(snap_dir() / "kr_universe.csv")
    u = u[u["kind"].eq("보통주")]
    rows = []
    for label, pre, s, e, cut, off_in, off_out in REVIEWS:
        w, fx = ewy_weights(pre)
        members = set(w.loc[w["weight"] >= P["msci_member_min_weight"], "code"])
        m = C.mcap_at(u, s, e).rename(columns={"mcap": "mcap_krw"})
        m = m.merge(u[["code", "market"]], on="code", how="left").sort_values("mcap_krw", ascending=False)
        pool = pd.concat([m[m["code"].isin(members)], m[~m["code"].isin(members)].head(80)]).drop_duplicates("code")
        pool = pool.merge(float_table(pool["code"].tolist()), on="code", how="left")
        pool = S._liq_room(pool, dt.date.fromisoformat(e))
        t = S.msci_classify(pool, members, P, cut * 1e12)
        p_in = set(t.loc[t["판정"].eq("편입 유력"), "code"])
        p_out = set(t.loc[t["판정"].str.startswith("편출 유력"), "code"])
        nm = dict(zip(t["code"], t["name"]))
        rows.append({"review": label, "C(조)": cut, "공식 편입": ", ".join(nm.get(c, c) for c in off_in),
                     "공식 편출": ", ".join(nm.get(c, c) for c in off_out),
                     "모델 편입": ", ".join(nm.get(c, c) for c in p_in), "모델 편출": ", ".join(nm.get(c, c) for c in p_out),
                     "편입 적중": f"{len(p_in & off_in)}/{len(off_in)}", "편출 적중": f"{len(p_out & off_out)}/{len(off_out)}",
                     "경합·위험": ", ".join(f"{r.name}[{r.판정}]" for r in t[t["판정"].str.contains("경합|위험|미달")].itertuples())})
        t.to_csv(snap_dir() / f"bt_msci_{label}.csv", index=False, encoding="utf-8-sig")
        log(f"[bt-msci] {label}: 편입 {len(p_in & off_in)}/{len(off_in)} 편출 {len(p_out & off_out)}/{len(off_out)} "
            f"오탐 편입 {len(p_in - off_in)} 편출 {len(p_out - off_out)}")
    return pd.DataFrame(rows)


if __name__ == "__main__":
    pd.set_option("display.width", 250)
    pd.set_option("display.max_colwidth", 400)
    print(run().T.to_string())
