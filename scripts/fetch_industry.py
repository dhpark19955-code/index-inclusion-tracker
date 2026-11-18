"""종목 → 네이버 업종(WICS 계열 79개 내외) 매핑. KOSPI200 산업군 근사용 (주 1회 갱신).

KRX 공식 산업군(KRICS) 종목별 결과가 공개되면(2026-10-26 예정) data/overrides/krics.csv 가 이 근사를 덮어쓴다.
"""
from __future__ import annotations

import time

import pandas as pd

from common import CACHE, get, load_json, save_json, log

PATH = CACHE / "naver_industry.json"


def run(max_age_days: int = 7) -> dict[str, str]:
    m = load_json(PATH)
    if m and (time.time() - PATH.stat().st_mtime) < max_age_days * 86400:
        return m
    groups, page = [], 1
    while True:  # pageSize 는 100 이 상한 (200 은 HTTP 400)
        g = get("https://m.stock.naver.com/api/stocks/industry", params={"page": page, "pageSize": 100}).json()["groups"]
        groups += g
        if len(g) < 100:
            break
        page += 1
    m = {}
    for g in groups:
        page = 1
        while True:
            j = get(f"https://m.stock.naver.com/api/stocks/industry/{g['no']}",
                    params={"page": page, "pageSize": 100}).json()
            st = j.get("stocks") or []
            for s in st:
                m[s["itemCode"]] = g["name"]
            if len(st) < 100:
                break
            page += 1
            time.sleep(0.1)
        time.sleep(0.1)
    save_json(m, PATH)
    log(f"[industry] 네이버 업종 {len(groups)}개, 종목 {len(m)}개 매핑")
    return m


if __name__ == "__main__":
    m = run(max_age_days=0)
    print(pd.Series(m).value_counts().to_string())
