"""한국 종목 지표: 기간 일평균 시총·거래대금(KOSPI200용), 현재 시총·유동비율 추정(MSCI용).

일별 시총 = 일별 종가 × 현재 상장주식수 (네이버 시총/종가 역산). 기간 중 증자·소각·분할이 있었던
종목은 오차가 생긴다 — KRX Open API(MKTCAP) 승인 후 교체 예정. 리포트에 근사임을 명시한다.
"""
from __future__ import annotations

import datetime as dt
import io
import re
import time
import zipfile
import xml.etree.ElementTree as ET

import pandas as pd

from common import CACHE, env, get, load_json, save_json, log

PX = CACHE / "prices_kr"
DART = CACHE / "dart"
DART.mkdir(parents=True, exist_ok=True)


def price_frame(code: str) -> pd.DataFrame | None:
    rows = load_json(PX / f"{code}.json")
    if not rows:
        return None
    df = pd.DataFrame(rows).iloc[:, :7]
    df.columns = ["date", "open", "high", "low", "close", "volume", "foreign_rate"][:df.shape[1]]
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    # 미완성 봉 제외: 지금이 장중(15:40 이전)이거나, 캐시를 장중에 받아 둔 채 마감 후 --no-fetch 로 다시 돌릴 때
    # (10-14: 09:50 수집분이 16:02 재실행에 그대로 쓰여 장중가가 섞였음)
    now = dt.datetime.now()
    saved = dt.datetime.fromtimestamp((PX / f"{code}.json").stat().st_mtime)
    close_cut = lambda t: t.hour * 60 + t.minute < 15 * 60 + 40
    if close_cut(now) or (saved.date() == now.date() and close_cut(saved)):
        df = df[df["date"].dt.date < now.date()]
    return df


def returns(code: str, days=(7, 30, 91)) -> dict:
    """최근 종가 대비 과거 d일(달력일) 전 종가 수익률. 캐시된 일봉 사용."""
    df = price_frame(code)
    out = {f"ret_{d}d": None for d in days}
    if df is None or len(df) < 2:
        return out
    last_dt, last_px = df["date"].iloc[-1], df["close"].iloc[-1]
    for d in days:
        w = df[df["date"] <= last_dt - pd.Timedelta(days=d)]
        if len(w) and w["close"].iloc[-1]:
            out[f"ret_{d}d"] = last_px / w["close"].iloc[-1] - 1
    return out


def window_stats(u: pd.DataFrame, start: dt.date, end: dt.date) -> pd.DataFrame:
    """[start, end] 구간 일평균 시총·거래대금 (상장 후 기간이 짧으면 상장일부터)."""
    recs = []
    for r in u.itertuples(index=False):
        df = price_frame(r.code)
        if df is None or not r.shares or pd.isna(r.shares):
            continue
        w = df[(df["date"] >= pd.Timestamp(start)) & (df["date"] <= pd.Timestamp(end))]
        if w.empty:
            continue
        mcap = w["close"] * r.shares
        tv = w["close"] * w["volume"]
        recs.append({"code": r.code, "avg_mcap": mcap.mean(), "avg_tv": tv.mean(), "days": len(w),
                     "first_date": df["date"].min().date(), "last_date": w["date"].max().date(),
                     "mcap_last": mcap.iloc[-1], "foreign_rate": w["foreign_rate"].iloc[-1] if "foreign_rate" in w else None})
    return pd.DataFrame(recs)


# ---------------- DART 유동비율 추정 ----------------
def corp_map() -> dict[str, str]:
    """종목코드 → DART corp_code (주 1회 갱신)."""
    path = DART / "corp_map.json"
    m = load_json(path)
    if m and (time.time() - path.stat().st_mtime) < 7 * 86400:
        return m
    key = env("DART_API_KEY")
    z = get("https://opendart.fss.or.kr/api/corpCode.xml", params={"crtfc_key": key}, timeout=90).content
    xml = zipfile.ZipFile(io.BytesIO(z)).read("CORPCODE.xml")
    m = {}
    for el in ET.fromstring(xml).iter("list"):
        sc = (el.findtext("stock_code") or "").strip()
        if sc:
            m[sc] = el.findtext("corp_code")
    save_json(m, path)
    return m


def _dart(ep: str, corp: str, year: int, rcode: str) -> list[dict]:
    path = DART / f"{corp}_{ep}_{year}_{rcode}.json"
    j = load_json(path)
    if j is None:
        r = get(f"https://opendart.fss.or.kr/api/{ep}.json",
                params={"crtfc_key": env("DART_API_KEY"), "corp_code": corp, "bsns_year": year, "reprt_code": rcode})
        j = r.json()
        if j.get("status") in ("000", "013"):   # 013 = 데이터 없음 (캐시해도 됨)
            save_json(j, path)
        time.sleep(0.12)
    return j.get("list") or []


def _f(x) -> float:
    try:
        return float(str(x).replace(",", "").strip())
    except ValueError:
        return 0.0


def float_estimate(code: str, cmap: dict) -> dict:
    """1 − (최대주주+특관 지분율 + 자기주식/발행주식) — 최신 정기보고서 기준. 기타 전략적 보유는 미반영."""
    corp = cmap.get(code)
    if not corp:
        return {"ff_note": "corp_code 없음"}
    today = dt.date.today()
    tries = [(today.year, "11014"), (today.year, "11012"), (today.year, "11013"), (today.year - 1, "11011")]
    for year, rc in tries:
        hy = _dart("hyslrSttus", corp, year, rc)
        if not hy:
            continue
        def knd(x):  # '의결권<줄바꿈>있는 주식' 처럼 줄바꿈이 섞여 온다
            return re.sub(r"\s+", "", x.get("stock_knd") or "")
        common = [x for x in hy if "보통" in knd(x) or "의결권있는" in knd(x)]
        tot = [x for x in common if (x.get("nm") or "").strip() in ("계", "합계")]
        major = _f(tot[0]["trmend_posesn_stock_qota_rt"]) if tot else sum(
            _f(x.get("trmend_posesn_stock_qota_rt")) for x in common)
        st = _dart("stockTotqySttus", corp, year, rc)
        row = next((x for x in st if (x.get("se") or "").strip() in ("보통주", "보통주식")), None)
        issued = _f(row.get("istc_totqy")) if row else 0
        treas = _f(row.get("tesstk_co")) if row else 0
        tr_ratio = treas / issued * 100 if issued else 0
        ff = max(0.0, 100 - major - tr_ratio)
        return {"major_pct": round(major, 2), "treasury_pct": round(tr_ratio, 2), "ff_est": round(ff, 1),
                "ff_src": f"{year}/{rc}"}
    return {"ff_note": "정기보고서 지분 데이터 없음"}


def float_table(codes: list[str]) -> pd.DataFrame:
    cmap = corp_map()
    recs = []
    for c in codes:
        try:
            recs.append({"code": c, **float_estimate(c, cmap)})
        except Exception as e:
            recs.append({"code": c, "ff_note": f"{type(e).__name__}"})
    log(f"[kr] DART 유동비율 추정 {len(recs)}종목")
    return pd.DataFrame(recs)
