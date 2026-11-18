"""MSCI Korea 기준선 역산: 과거 반기리뷰의 실제 편입/편출(EWY 보유종목 전후 비교)과 당시 시총 비교.

  python calibrate_msci.py            # 2025-11, 2026-05 반기리뷰 + 2026-02, 2026-08 분기리뷰
출력: 리뷰별 편입종목 최소 시총 / 편출종목 최대 시총 / 유지된 최소 구성종목 / 미편입 최대 종목 (USD)
"""
from __future__ import annotations

import datetime as dt
import io

import pandas as pd

from common import get, snap_dir, read_csv, log
from kr_metrics import price_frame

EWY = ("https://www.blackrock.com/varnish-api/blk-one01-product-data/product-data/api/v1/get-fund-document"
       "?appType=PRODUCT_PAGE&appSubType=ISHARES&targetSite=us-ishares&locale=en_US&portfolioId=239681"
       "&userType=individual&asOfDate={d}&component=holdings")
NAME_MAP = {"ECOPRO BM CO LTD": "247540", "SAMSUNG BIOLOGICS CO LTD": "207940"}

# (리뷰, 직전 보유일, 직후 보유일, 가격 판정구간 시작, 끝)
REVIEWS = [
    ("2025-11 SAIR", "20251114", "20251205", "2025-10-17", "2025-10-31"),
    ("2026-02 QIR", "20260213", "20260306", "2026-01-16", "2026-01-30"),
    ("2026-05 SAIR", "20260515", "20260605", "2026-04-17", "2026-04-30"),
    ("2026-08 QIR", "20260814", "20260904", "2026-07-17", "2026-07-31"),
]


def ewy(d: str) -> tuple[dict, float]:
    lines = get(EWY.format(d=d)).text.splitlines()
    h = next(i for i, l in enumerate(lines) if l.startswith("Ticker,"))
    df = pd.read_csv(io.StringIO("\n".join(lines[h:])), dtype=str)
    df = df[df["Asset Class"].eq("Equity") & ~df["Name"].str.contains("RIGHTS", na=False)]
    codes = {}
    for t, n in zip(df["Ticker"], df["Name"]):
        c = t if isinstance(t, str) and len(t) == 6 and t[:5].isalnum() and not t.endswith("D") else NAME_MAP.get(n)
        if c:
            codes[c] = n
    fx = pd.to_numeric(df["FX Rate"].str.replace(",", ""), errors="coerce").median()
    return codes, float(fx)


def mcap_at(u: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    recs = []
    for r in u.itertuples(index=False):
        df = price_frame(r.code)
        if df is None or not r.shares:
            continue
        w = df[(df["date"] >= start) & (df["date"] <= end)]
        if len(w):
            recs.append({"code": r.code, "name": r.name, "mcap": w["close"].mean() * r.shares})
    return pd.DataFrame(recs)


def run():
    u = read_csv(snap_dir() / "kr_universe.csv")
    u = u[u["kind"].eq("보통주")]
    out = []
    for label, pre, post, s, e in REVIEWS:
        A, fx = ewy(pre)
        B, _ = ewy(post)
        ins, outs = set(B) - set(A), set(A) - set(B)
        m = mcap_at(u, s, e)
        m["usd_bn"] = m["mcap"] / fx / 1e9
        m["member"] = m["code"].isin(A)
        m = m.sort_values("usd_bn", ascending=False)
        add = m[m.code.isin(ins)]
        dele = m[m.code.isin(outs)]
        kept = m[m.member & ~m.code.isin(outs)]
        nonadd = m[~m.member & ~m.code.isin(ins)]
        row = {"review": label, "fx": round(fx, 1),
               "adds": ", ".join(f"{r.name}({r.usd_bn:.2f})" for r in add.itertuples()),
               "dels": ", ".join(f"{r.name}({r.usd_bn:.2f})" for r in dele.itertuples()),
               "min_add_usd_bn": add["usd_bn"].min() if len(add) else None,
               "max_del_usd_bn": dele["usd_bn"].max() if len(dele) else None,
               "min_kept_usd_bn": kept["usd_bn"].min(),
               "kept_bottom3": ", ".join(f"{r.name}({r.usd_bn:.2f})" for r in kept.tail(3).itertuples()),
               "top_nonadded": ", ".join(f"{r.name}({r.usd_bn:.2f})" for r in nonadd.head(6).itertuples())}
        out.append(row)
        log(f"[calib] {label}: +{len(add)} −{len(dele)} fx {fx:.1f}")
    df = pd.DataFrame(out)
    df.to_csv(snap_dir() / "msci_calibration.csv", index=False, encoding="utf-8-sig")
    return df


if __name__ == "__main__":
    pd.set_option("display.width", 250)
    pd.set_option("display.max_colwidth", 200)
    print(run().T.to_string())
