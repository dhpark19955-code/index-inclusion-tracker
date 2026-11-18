"""MSCI Korea 커버리지 기준선 진단: 리뷰별 '유동시총 누적 85%' 지점의 전체시총(USD)과 실제 편입/편출 배수."""
from __future__ import annotations

import pandas as pd

from common import snap_dir, read_csv, log
from kr_metrics import float_table
import calibrate_msci as C

COVERAGE = 0.85


def run(extra=(("2026-11 SAIR(현재)", "20261009", "20261009", "2026-09-28", "2026-10-08"),)):
    u = read_csv(snap_dir() / "kr_universe.csv")
    u = u[u["kind"].eq("보통주")]
    ff = float_table(u.head(320)["code"].tolist()).set_index("code")["ff_est"]
    rows = []
    for label, pre, post, s, e in list(C.REVIEWS) + list(extra):
        A, fx = C.ewy(pre)
        B, _ = C.ewy(post)
        m = C.mcap_at(u, s, e)
        tail = u[~u["code"].isin(m["code"])][["code", "name", "mcap_krw"]].rename(columns={"mcap_krw": "mcap"})
        m = pd.concat([m, tail], ignore_index=True)
        m["ff"] = m["code"].map(ff).fillna(55) / 100
        m["ffm"] = m["mcap"] * m["ff"]
        m = m.sort_values("mcap", ascending=False).reset_index(drop=True)
        m["cov"] = m["ffm"].cumsum() / m["ffm"].sum()
        m["usd"] = m["mcap"] / fx / 1e9
        i = int((m["cov"] >= COVERAGE).idxmax())
        cut = float(m.loc[i, "usd"])
        ins, outs = set(B) - set(A), set(A) - set(B)
        fmt = lambda d: ", ".join(f"{x.name}({x.usd / cut:.2f}x/ff{x.ff:.0%})" for x in d.itertuples())
        kept = m[m["code"].isin(set(A) - outs)]
        rows.append({
            "review": label, "fx": round(fx, 1), "cutoff_usd_bn": round(cut, 2), "cutoff_rank": i + 1,
            "cutoff_name": m.loc[i, "name"], "top2_ff_share": round(m["ffm"].head(2).sum() / m["ffm"].sum(), 3),
            "adds(x cutoff)": fmt(m[m["code"].isin(ins)]), "dels(x cutoff)": fmt(m[m["code"].isin(outs)]),
            "kept_bottom4": fmt(kept.tail(4)), "top_nonmembers": fmt(m[~m["code"].isin(set(A) | ins)].head(8)),
        })
        log(f"[coverage] {label}: cutoff ${cut:.2f}B rank {i + 1}")
    df = pd.DataFrame(rows)
    df.to_csv(snap_dir() / "msci_coverage.csv", index=False, encoding="utf-8-sig")
    return df


if __name__ == "__main__":
    pd.set_option("display.width", 250)
    pd.set_option("display.max_colwidth", 400)
    print(run().T.to_string())
