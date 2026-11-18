"""KOSPI200 모델 백테스트: 과거 정기변경 직전 구성종목(KODEX 200 PDF)으로 모델을 돌려 실제 변경과 대조.

  python backtest_kospi200.py
"""
from __future__ import annotations

import datetime as dt
import re

import pandas as pd

from common import get, snap_dir, read_csv, log
from fetch_industry import run as naver_industry
import kospi200_model as K

REVIEWS = [  # 라벨, 심사기준일, 직전 PDF, 직후 PDF
    ("2025-12", dt.date(2025, 10, 31), "20251120", "20260105"),
    ("2026-06", dt.date(2026, 4, 30), "20260520", "20260701"),
]


def pdf_members(d: str) -> dict[str, str]:
    p = get("https://www.samsungfund.com/api/v1/kodex/product-pdf/2ETF01.do", params={"gijunYMD": d}).json()["pdf"]
    return {x["itmNo"]: x["secNm"] for x in p["list"]
            if re.fullmatch(r"[0-9A-Z]{6}", str(x.get("itmNo", ""))) and x.get("curp")}


def universe() -> pd.DataFrame:
    u = read_csv(snap_dir() / "kr_universe.csv")
    return u[u["market"].eq("KOSPI") & u["kind"].eq("보통주")][["code", "name", "shares"]]


def run(scheme: str = "GICS10") -> pd.DataFrame:
    u = universe()
    groups = K.group_map(scheme, naver_industry())
    rows = []
    for label, base, pre, post in REVIEWS:
        A, B = pdf_members(pre), pdf_members(post)
        res = K.select(u, set(A), groups, base)
        pred_in = set(res.loc[res["판정_raw"].eq("편입"), "code"])
        pred_out = set(res.loc[res["판정_raw"].str.startswith("편출"), "code"])
        act_in, act_out = set(B) - set(A), set(A) - set(B)
        nm = {**A, **B, **dict(zip(u["code"], u["name"]))}
        hit_in, hit_out = pred_in & act_in, pred_out & act_out
        rows.append({
            "review": label, "n_sel": res.attrs["n_sel"],
            "actual_in": ", ".join(nm.get(c, c) for c in act_in), "actual_out": ", ".join(nm.get(c, c) for c in act_out),
            "pred_in": ", ".join(nm.get(c, c) for c in pred_in), "pred_out": ", ".join(nm.get(c, c) for c in pred_out),
            "hit_in": f"{len(hit_in)}/{len(act_in)}", "hit_out": f"{len(hit_out)}/{len(act_out)}",
            "false_in": ", ".join(nm.get(c, c) for c in pred_in - act_in),
            "false_out": ", ".join(nm.get(c, c) for c in pred_out - act_out),
        })
        detail = res[res["code"].isin(act_in | act_out | pred_in | pred_out)].copy()
        detail["actual"] = detail["code"].map(lambda c: "IN" if c in act_in else "OUT" if c in act_out else "")
        detail.to_csv(snap_dir() / f"bt_kospi200_{label}_{scheme}.csv", index=False, encoding="utf-8-sig")
        log(f"[bt] {label} {scheme}: 편입 적중 {len(hit_in)}/{len(act_in)} 편출 적중 {len(hit_out)}/{len(act_out)} "
            f"(오탐 편입 {len(pred_in - act_in)}, 편출 {len(pred_out - act_out)})")
    return pd.DataFrame(rows)


if __name__ == "__main__":
    pd.set_option("display.width", 250)
    pd.set_option("display.max_colwidth", 300)
    print(run().T.to_string())
