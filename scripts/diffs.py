"""스냅샷 간 비교: (1) 실제 구성종목 변경 감지 (2) 예측 리스트 변화."""
from __future__ import annotations

import pandas as pd

from common import SNAP, snap_dir, latest_snap_before, today, read_csv

KEY = {"kospi200": "code", "msci_korea": "code", "ndx": "symbol", "sp500": "symbol"}
LABEL = {"kospi200": "KOSPI200", "msci_korea": "MSCI Korea", "ndx": "Nasdaq-100", "sp500": "S&P 500"}


def constituent_changes() -> pd.DataFrame:
    """오늘 스냅샷 vs 직전 스냅샷 구성종목 차이 (ETF 반영 기준이라 효력일 전후에 잡힌다)."""
    recs = []
    for k, col in KEY.items():
        cur_p = snap_dir() / f"const_{k}.csv"
        prev_p = latest_snap_before(f"const_{k}.csv", today())
        if not cur_p.exists() or prev_p is None:
            continue
        cur, prev = read_csv(cur_p), read_csv(prev_p)
        cn, pn = dict(zip(cur[col], cur["name"])), dict(zip(prev[col], prev["name"]))
        for s in sorted(set(cn) - set(pn)):
            recs.append({"지수": LABEL[k], "구분": "편입 확정(반영)", "종목": s, "종목명": cn[s], "비교기준": prev_p.parent.name})
        for s in sorted(set(pn) - set(cn)):
            recs.append({"지수": LABEL[k], "구분": "편출 확정(반영)", "종목": s, "종목명": pn[s], "비교기준": prev_p.parent.name})
    return pd.DataFrame(recs, columns=["지수", "구분", "종목", "종목명", "비교기준"])


def save_predictions(pred: pd.DataFrame):
    pred.to_csv(snap_dir() / "predictions.csv", index=False, encoding="utf-8-sig")


def prediction_changes(pred: pd.DataFrame) -> pd.DataFrame:
    """pred: [지수, 종목, 종목명, 판정]. 직전 실행 대비 새로 들어온/빠진/판정이 바뀐 종목."""
    prev_p = latest_snap_before("predictions.csv", today())
    if prev_p is None:
        return pd.DataFrame(columns=["지수", "종목", "종목명", "이전", "현재", "비교기준"])
    prev = read_csv(prev_p, dtype=str).fillna("")
    cur = pred.astype(str)
    m = cur.merge(prev, on=["지수", "종목"], how="outer", suffixes=("", "_prev")).fillna("")
    m = m[m["판정"] != m["판정_prev"]]
    out = pd.DataFrame({"지수": m["지수"], "종목": m["종목"],
                        "종목명": m["종목명"].where(m["종목명"] != "", m["종목명_prev"]),
                        "이전": m["판정_prev"].replace("", "(없음)"), "현재": m["판정"].replace("", "(없음)"),
                        "비교기준": prev_p.parent.name})
    return out.sort_values(["지수", "현재"]).reset_index(drop=True)
