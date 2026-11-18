"""한국 전 종목 시총 스냅샷 + 일봉 이력 캐시.

- 시총 순위: m.stock.naver.com/api/stocks/marketValue/{KOSPI|KOSDAQ} (marketValue=억원, 거래대금=백만원)
- 일봉: api.finance.naver.com/siseJson (종가·거래량·외국인소진율), 종목별 캐시 + 증분 갱신
- 상장주식수 = 시총 / 종가 (현재값). 과거 일별 시총 = 과거 종가 × 현재 주식수 (근사, 리포트에 명시)

KRX Open API 승인 후에는 일별 MKTCAP 으로 교체할 수 있게 hist 인터페이스만 맞춰 둔다.
"""
from __future__ import annotations

import ast
import datetime as dt
import re
import time

import pandas as pd

from common import CACHE, get, num, today, ymd, snap_dir, load_json, save_json, log

PX = CACHE / "prices_kr"
PX.mkdir(parents=True, exist_ok=True)


def classify(code: str, name: str) -> str:
    """보통주 외 종목 판별 (지수 심사 대상 제외용)."""
    if not code.endswith("0"):          # 보통주 코드는 끝자리 0 (신형 0126Z0 포함)
        return "우선주"
    if "스팩" in name:
        return "스팩"
    if "클래스" in name or name.endswith("인덱스"):
        return "펀드"
    if "리츠" in name or "REIT" in name.upper():
        return "리츠"
    if name.endswith("인프라") or "선박투자" in name:
        return "인프라/선박"
    return "보통주"


def universe() -> pd.DataFrame:
    frames = []
    for mkt in ("KOSPI", "KOSDAQ"):
        page, rows = 1, []
        while True:
            j = get(f"https://m.stock.naver.com/api/stocks/marketValue/{mkt}",
                    params={"page": page, "pageSize": 100}).json()
            st = j.get("stocks") or []
            rows += st
            if not st or page * 100 >= j.get("totalCount", 0):
                break
            page += 1
            time.sleep(0.15)
        df = pd.DataFrame(rows)
        df["market"] = mkt
        frames.append(df)
    u = pd.concat(frames, ignore_index=True)
    u = u[u["stockEndType"].eq("stock")].copy()
    out = pd.DataFrame({
        "code": u["itemCode"], "name": u["stockName"], "market": u["market"],
        "close": u["closePrice"].map(num),
        "mcap_krw": u["marketValue"].map(num) * 1e8,           # 억원 → 원
        "tv_krw": u["accumulatedTradingValue"].map(num) * 1e6,  # 백만원 → 원
        "traded_at": u["localTradedAt"].str[:10],
    })
    out["shares"] = (out["mcap_krw"] / out["close"]).round()
    out["kind"] = [classify(c, n) for c, n in zip(out["code"], out["name"])]
    # 장중에는 페이지를 넘기는 사이 순위가 바뀌어 중복·누락이 생긴다 → 중복 제거 (정규 실행은 장 마감 후)
    out = out.drop_duplicates("code").sort_values("mcap_krw", ascending=False).reset_index(drop=True)
    return out


def _sise(code: str, start: str, end: str) -> list[list]:
    r = get("https://api.finance.naver.com/siseJson.naver",
            params={"symbol": code, "requestType": 1, "startTime": start, "endTime": end, "timeframe": "day"})
    txt = r.text.strip()
    if not txt:
        return []
    rows = ast.literal_eval(re.sub(r"\s+", "", txt))
    return [x for x in rows[1:] if isinstance(x, list) and len(x) >= 6]


def history(code: str, days: int = 560, refresh_tail: int = 7) -> pd.DataFrame:
    """일봉 캐시(date, close, volume, foreign_rate). 캐시가 있으면 최근 refresh_tail일만 다시 받는다.

    days=560: 직전 정기변경(심사기준일 −1년) 백테스트까지 덮는 길이.
    """
    path = PX / f"{code}.json"
    cached = load_json(path, default=[]) or []
    end = ymd(today())
    want_start = ymd(today() - dt.timedelta(days=days))
    if cached and cached[0][0] > ymd(today() - dt.timedelta(days=days - 10)):
        # 캐시가 원하는 길이보다 짧으면 앞부분을 보충 (신규상장이면 원래 짧음 — 그대로 둔다)
        older = _sise(code, want_start, cached[0][0])
        cached = [x for x in older if x[0] < cached[0][0]] + cached
    if cached:
        last = dt.datetime.strptime(cached[-1][0], "%Y%m%d").date()
        start = ymd(last - dt.timedelta(days=refresh_tail))
        new = _sise(code, start, end)
        keep = [x for x in cached if x[0] < start]
        rows = keep + new
    else:
        rows = _sise(code, ymd(today() - dt.timedelta(days=days)), end)
    cutoff = ymd(today() - dt.timedelta(days=days))
    rows = [x for x in rows if x[0] >= cutoff]
    save_json(rows, path)
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "foreign_rate"][:len(rows[0])] if rows else
                      ["date", "open", "high", "low", "close", "volume", "foreign_rate"])
    return df


def run(need_codes: set[str] | None = None, top_kospi: int = 0, top_kosdaq: int = 150) -> pd.DataFrame:
    u = universe()
    sd = snap_dir()
    u.to_csv(sd / "kr_universe.csv", index=False, encoding="utf-8-sig")
    log(f"[kr] universe {len(u)}종목 (KOSPI {sum(u.market == 'KOSPI')}, KOSDAQ {sum(u.market == 'KOSDAQ')}), "
        f"기준 {u['traded_at'].max()}")
    common = u[u["kind"].eq("보통주")]
    kospi = common[common.market.eq("KOSPI")]
    codes = set((kospi.head(top_kospi) if top_kospi else kospi)["code"])  # 0 → 코스피 보통주 전부 (KOSPI200 산업군 계산용)
    codes |= set(common[common.market.eq("KOSDAQ")].head(top_kosdaq)["code"])
    codes |= set(need_codes or ())
    n_ok = n_fail = 0
    for i, c in enumerate(sorted(codes)):
        try:
            history(c)
            n_ok += 1
        except Exception as e:
            n_fail += 1
            log(f"[kr] history {c} 실패: {type(e).__name__} {e}")
        time.sleep(0.08)
    log(f"[kr] 일봉 캐시 갱신 {n_ok}종목, 실패 {n_fail}")
    return u


if __name__ == "__main__":
    run()
