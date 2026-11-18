"""스냅샷·일정·공식 발표를 모아 GitHub Pages 대시보드(index.html)가 읽는 data.json 을 만든다.

사용: python -I -X utf8 scripts/build_site_data.py [--snap YYYYMMDD]
입력: data/snapshots/<snap>/predictions.csv, data/overrides/calendar.csv, data/overrides/official_changes.csv
출력: data.json (저장소 루트)
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAPS = ROOT / "data" / "snapshots"
OVR = ROOT / "data" / "overrides"

INDEX_ORDER = ["KOSPI200", "MSCI Korea", "MSCI Japan", "FTSE", "Nasdaq-100", "S&P 500"]

# 과거 리뷰 재현 결과 (scripts/backtest_*.py 실행값, 2026-10-12~14)
BACKTESTS = [
    {"index": "KOSPI200", "review": "2026-06", "add": [4, 4], "drop": [4, 4], "note": "오판 1건. 산업군 규칙이 결과를 결정"},
    {"index": "KOSPI200", "review": "2025-12", "add": [7, 10], "drop": [6, 9], "note": "놓친 종목 대부분이 합병·분할 수시변경과 겹친 경우"},
    {"index": "MSCI Korea", "review": "2026-08", "add": [1, 1], "drop": [4, 4], "note": "편출 4건 중 2건은 유동시총 미달 - 규칙 그대로 재현"},
    {"index": "MSCI Korea", "review": "2025-11", "add": [1, 2], "drop": [1, 1], "note": "놓친 1건은 편입선 바로 아래 '경합'"},
    {"index": "Nasdaq-100", "review": "2026-06", "add": [5, 5], "drop": [5, 5], "note": "분기 변경(125위 밖 편출) 완전 재현"},
    {"index": "Nasdaq-100", "review": "2025-12", "add": [6, 6], "drop": [5, 6], "note": "놓친 BIIB 가 버퍼 자격 규칙을 드러내 모델에 반영"},
    {"index": "MSCI Japan", "review": "워크포워드 3회", "add": [4, 6], "drop": [8, 15], "note": "종목 수 조정 규칙으로 편출 5/15 -> 8/15"},
]

# 공식 발표가 '변경 없음'이라 official_changes.csv 에 행이 없는 리뷰
NO_CHANGE_REVIEWS = [
    {"index": "MSCI Korea", "review": "2026-11 반기리뷰", "announced": "2026-11-12", "effective": "2026-12-01",
     "source": "MSCI 2026-11 Index Review (언론 보도)",
     "note": "스탠다드 지수 편입·편출 없음 (스몰캡만 5 편입·4 편출)"},
]


def read_csv(p: Path) -> list[dict]:
    with open(p, encoding="utf-8-sig", newline="") as f:
        return [dict(r) for r in csv.DictReader(f)]


def side(verdict: str) -> str:
    if verdict.startswith(("편입", "승격", "Fast")):
        return "add"
    if verdict.startswith(("편출", "강등")):
        return "drop"
    return "other"


def strength(verdict: str) -> str:
    if "유력" in verdict or verdict.startswith("Fast"):
        return "strong"
    if any(k in verdict for k in ("경합", "위험", "후보", "관찰", "근접", "경계")):
        return "watch"
    return "info"


def compare(preds: list[dict], official: list[dict], index: str) -> dict:
    pmap = {r["종목"]: r for r in preds if r["지수"] == index}
    off = [r for r in official if r["지수"] == index]
    out: dict = {"adds": [], "drops": []}
    for r in off:
        p = pmap.get(r["종목"])
        verdict = p["판정"] if p else ""
        status = "miss"
        if p:
            if side(verdict) == ("add" if r["구분"] == "편입" else "drop"):
                status = "hit_strong" if strength(verdict) == "strong" else "hit_watch"
            else:
                status = "wrong_side"
        out["adds" if r["구분"] == "편입" else "drops"].append(
            {"code": r["종목"], "name": r["종목명"], "verdict": verdict or "(판정 없음)", "status": status})
    off_codes = {r["종목"] for r in off}
    out["false_strong"] = [{"code": c, "name": p["종목명"], "verdict": p["판정"]}
                           for c, p in pmap.items() if strength(p["판정"]) == "strong" and c not in off_codes]
    out["false_watch_n"] = sum(1 for c, p in pmap.items() if strength(p["판정"]) == "watch" and c not in off_codes)

    def tally(rows):
        return {"n": len(rows), "strong": sum(r["status"] == "hit_strong" for r in rows),
                "watch": sum(r["status"] == "hit_watch" for r in rows),
                "miss": sum(r["status"] in ("miss", "wrong_side") for r in rows)}
    out["summary"] = {"adds": tally(out["adds"]), "drops": tally(out["drops"])}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snap", default=sorted(p.name for p in SNAPS.iterdir() if p.is_dir())[-1])
    a = ap.parse_args()
    snap = SNAPS / a.snap
    preds = read_csv(snap / "predictions.csv")
    cal = read_csv(OVR / "calendar.csv")
    official = read_csv(OVR / "official_changes.csv")

    predictions: dict[str, list[dict]] = {}
    for idx in INDEX_ORDER:
        rows = [r for r in preds if r["지수"] == idx]
        predictions[idx] = [{"code": r["종목"], "name": r["종목명"], "verdict": r["판정"],
                             "side": side(r["판정"]), "strength": strength(r["판정"])} for r in rows]

    counts = []
    for idx in INDEX_ORDER:
        c: dict[str, int] = {}
        for r in predictions[idx]:
            c[r["verdict"]] = c.get(r["verdict"], 0) + 1
        counts.append({"index": idx, "n": len(predictions[idx]),
                       "strong_add": sum(r["side"] == "add" and r["strength"] == "strong" for r in predictions[idx]),
                       "strong_drop": sum(r["side"] == "drop" and r["strength"] == "strong" for r in predictions[idx]),
                       "verdicts": c})

    reviews = []
    for idx in INDEX_ORDER:
        rows = [r for r in official if r["지수"] == idx]
        if rows:
            o = rows[0]
            cmp_ = compare(preds, official, idx)
            cmp_.update({"index": idx, "review": f"{o['효력일'][:7]} 정기변경", "announced": o["발표일"],
                         "effective": o["효력일"], "source": o["출처"]})
            reviews.append(cmp_)
    for nc in NO_CHANGE_REVIEWS:
        pm = predictions[nc["index"]]
        reviews.append({**nc, "adds": [], "drops": [], "no_change": True,
                        "false_strong": [{"code": r["code"], "name": r["name"], "verdict": r["verdict"]}
                                         for r in pm if r["strength"] == "strong"],
                        "watch_not_realized": [{"code": r["code"], "name": r["name"], "verdict": r["verdict"]}
                                               for r in pm if r["strength"] == "watch"],
                        "summary": {"adds": {"n": 0, "strong": 0, "watch": 0, "miss": 0},
                                    "drops": {"n": 0, "strong": 0, "watch": 0, "miss": 0}}})

    data = {
        "as_of": f"{a.snap[:4]}-{a.snap[4:6]}-{a.snap[6:]}",
        "built": dt.date.today().isoformat(),
        "index_order": INDEX_ORDER,
        "counts": counts,
        "predictions": predictions,
        "calendar": [{"index": r["index"], "event": r["event"], "date": r["date"], "kst_note": r["kst_note"],
                      "kind": r["kind"], "status": r["status"], "source": r["source"]} for r in cal],
        "backtests": BACKTESTS,
        "reviews": reviews,
    }
    (ROOT / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    for r in reviews:
        s = r["summary"]
        print(f"{r['index']} {r['review']}: adds {s['adds']} drops {s['drops']} "
              f"false_strong={[x['name'] for x in r.get('false_strong', [])]} false_watch_n={r.get('false_watch_n')}")
    print("data.json written:", len(preds), "predictions")


if __name__ == "__main__":
    main()
