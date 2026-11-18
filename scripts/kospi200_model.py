"""KOSPI200 정기변경 선정 모델 — KRX 「KOSPI 200 지수 방법론」(2026-11-02 시행본) §6 구현.

  심사기준일: 변경월 전전월 최종 매매일 (12월 변경 → 10월 말)
  집계구간  : 심사기준일이 속한 달 포함 소급 6개월 (각 월 첫~마지막 매매일)
  심사대상  : 코스피 보통주 − 관리종목·리츠·선박·인프라·스팩 − 유동비율 10% 미만 − 상장 6개월 미만
  유동성    : 산업군 내 일평균거래대금 순위가 산업군 심사대상수의 85% 이내
  1차       : 산업군별 일평균시총 큰 순 누적 85% 도달 종목까지 (유동성 미달 제외)
  2차       : 기존 구성종목은 산업군 내 순위가 산업군 기존 구성종목수의 110% 이내면 유지,
              신규는 1차 선정 + 90% 이내일 때만 (기존 구성종목 3개 미만 산업군은 예외)
  3차       : 200개 미만이면 미선정 중 시총 큰 순(유동성 충족) 추가, 초과면 시총 작은 순 제외 — 산업군 무관
  대형주특례: 15거래일 평균 시총 50위 이내 미선정 종목은 편입하고 최소 시총 선정종목 제외

산업군: 2026-06 까지 GICS 참고 10개, 2026-11-02 부터 KRX 산업분류(KRICS) 9개.
종목별 공식 분류는 비공개(KRICS 는 2026-10-26 공개 예정) → 네이버 업종(WICS 계열)으로 근사하고
data/overrides/krics.csv(code, group) 가 있으면 그 값을 우선한다.
"""
from __future__ import annotations

import datetime as dt
import math

import pandas as pd

from common import OVR
from kr_metrics import price_frame

# ---- 네이버 업종 → 산업군 근사 ----
GICS10 = {
    "에너지": ["석유와가스", "에너지장비및서비스"],
    "소재": ["화학", "철강", "비철금속", "포장재", "종이와목재", "건축자재"],
    "산업재": ["기계", "건설", "전기장비", "전기제품", "우주항공과국방", "상업서비스와공급품", "조선", "복합기업",
             "항공화물운송과물류", "해운사", "항공사", "도로와철도운송", "운송인프라", "무역회사와판매업체", "건축제품"],
    "자유소비재": ["자동차부품", "자동차", "섬유,의류,신발,호화품", "호텔,레스토랑,레저", "가정용기기와용품", "레저용장비와제품",
               "백화점과일반상점", "인터넷과카탈로그소매", "전문소매", "판매업체", "교육서비스", "가구", "다각화된소비자서비스",
               "전자제품", "문구류"],
    "필수소비재": ["식품", "음료", "화장품", "담배", "식품과기본식료품소매", "가정용품"],
    "헬스케어": ["제약", "생물공학", "건강관리장비와용품", "생명과학도구및서비스", "건강관리업체및서비스", "건강관리기술"],
    "금융및부동산": ["증권", "은행", "손해보험", "생명보험", "기타금융", "카드", "창업투자", "부동산"],
    "정보기술": ["반도체와반도체장비", "IT서비스", "전자장비와기기", "소프트웨어", "디스플레이장비및부품", "디스플레이패널",
             "핸드셋", "통신장비", "컴퓨터와주변기기", "사무용전자제품"],
    "커뮤니케이션서비스": ["방송과엔터테인먼트", "게임엔터테인먼트", "광고", "양방향미디어와서비스", "출판", "무선통신서비스",
                    "다각화된통신서비스"],
    "유틸리티": ["전기유틸리티", "가스유틸리티", "복합유틸리티"],
}
KRICS9 = {  # KRX 산업분류 9개 산업군 — 정의 미공개 구간은 추정(리포트에 '근사' 명시)
    "에너지화학": ["석유와가스", "에너지장비및서비스", "화학", "전기유틸리티", "가스유틸리티", "복합유틸리티"],
    "소재": ["철강", "비철금속", "포장재", "종이와목재", "건축자재"],
    "산업재": ["기계", "건설", "전기장비", "우주항공과국방", "상업서비스와공급품", "조선", "복합기업", "건축제품",
             "무역회사와판매업체"],
    "모빌리티": ["자동차", "자동차부품", "항공사", "해운사", "항공화물운송과물류", "도로와철도운송", "운송인프라"],
    "정보기술": ["반도체와반도체장비", "IT서비스", "전자장비와기기", "소프트웨어", "디스플레이장비및부품", "디스플레이패널",
             "핸드셋", "통신장비", "컴퓨터와주변기기", "전자제품", "사무용전자제품", "전기제품"],
    "금융및부동산": ["증권", "은행", "손해보험", "생명보험", "기타금융", "카드", "창업투자", "부동산"],
    "소비재": ["섬유,의류,신발,호화품", "호텔,레스토랑,레저", "가정용기기와용품", "레저용장비와제품", "백화점과일반상점",
            "인터넷과카탈로그소매", "전문소매", "판매업체", "교육서비스", "가구", "다각화된소비자서비스", "문구류",
            "식품", "음료", "화장품", "담배", "식품과기본식료품소매", "가정용품"],
    "헬스케어": ["제약", "생물공학", "건강관리장비와용품", "생명과학도구및서비스", "건강관리업체및서비스", "건강관리기술"],
    "미디어및콘텐츠": ["방송과엔터테인먼트", "게임엔터테인먼트", "광고", "양방향미디어와서비스", "출판", "무선통신서비스",
                  "다각화된통신서비스"],
}


def group_map(scheme: str, naver: dict[str, str]) -> dict[str, str]:
    table = GICS10 if scheme == "GICS10" else KRICS9
    inv = {ind: g for g, inds in table.items() for ind in inds}
    m = {c: inv.get(ind, "미분류") for c, ind in naver.items()}
    # ① 네이버 업종이 GICS/KRICS 와 어긋나는 종목 수동 보정 → ② KRX 공식 분류(krics.csv, 10/26 공개분)가 최우선
    files = ["gics10_fix.csv"] if scheme == "GICS10" else ["krics_fix.csv", "krics.csv"]
    for f in files:
        p = OVR / f
        if p.exists():
            o = pd.read_csv(p, dtype=str).dropna(subset=["code", "group"])
            m.update(dict(zip(o["code"], o["group"])))
    return m


def window(base: dt.date) -> tuple[dt.date, dt.date]:
    """심사기준일이 속한 달 포함 6개월: (5개월 전 1일, 기준일)."""
    y, mth = base.year, base.month - 5
    while mth <= 0:
        mth += 12
        y -= 1
    return dt.date(y, mth, 1), base


def stats(universe: pd.DataFrame, start: dt.date, end: dt.date, base: dt.date) -> pd.DataFrame:
    recs = []
    six_m = base - dt.timedelta(days=183)
    for r in universe.itertuples(index=False):
        df = price_frame(r.code)
        if df is None or not r.shares or pd.isna(r.shares):
            continue
        listed = df["date"].min().date()
        w = df[(df["date"] >= pd.Timestamp(start)) & (df["date"] <= pd.Timestamp(end))]
        w15 = df[df["date"] <= pd.Timestamp(end)].tail(15)
        if w.empty:
            continue
        recs.append({"code": r.code, "avg_mcap": (w["close"] * r.shares).mean(),
                     "avg_tv": (w["close"] * w["volume"]).mean(), "mcap15": (w15["close"] * r.shares).mean(),
                     "days": len(w), "listed_ok": listed <= six_m or listed <= dt.date(2025, 4, 2)})
        # 캐시가 2025-03-31 부터라 그 이전 상장 종목은 first date 가 캐시 시작일로 보임 → 상장 6개월 충족으로 처리
    return pd.DataFrame(recs)


def select(universe: pd.DataFrame, members: set[str], groups: dict[str, str], base: dt.date,
           asof: dt.date | None = None, ff: dict[str, float] | None = None, size: int = 200) -> pd.DataFrame:
    """universe: code,name,shares (코스피 보통주, 리츠·스팩 등 제외 후). 반환: 종목별 판정 테이블."""
    universe = universe.drop_duplicates("code")
    start, end = window(base)
    end = min(end, asof or end)
    st = stats(universe, start, end, base)
    x = universe[["code", "name"]].merge(st, on="code", how="inner")
    x["member"] = x["code"].isin(members)
    x["group"] = x["code"].map(groups).fillna("미분류")
    if ff:
        x["ff_ok"] = x["code"].map(lambda c: ff.get(c, 100) >= 10)
    else:
        x["ff_ok"] = True
    # 상장 6개월 미만이라도 분할 신설·특례 편입으로 이미 구성종목이면 심사대상 (§6.1 예외)
    x["eligible"] = (x["listed_ok"] | x["member"]) & x["ff_ok"]
    e = x[x["eligible"]].copy()

    out = []
    for g, d in e.groupby("group"):
        d = d.sort_values("avg_mcap", ascending=False).copy()
        n_all = len(d)
        d["g_rank"] = range(1, n_all + 1)
        d["g_tv_rank"] = d["avg_tv"].rank(ascending=False, method="first")
        d["liq_ok"] = d["g_tv_rank"] <= 0.85 * n_all
        cum = d["avg_mcap"].cumsum() / d["avg_mcap"].sum()
        first_cross = int((cum >= 0.85).values.argmax()) + 1
        d["s1"] = (d["g_rank"] <= first_cross) & d["liq_ok"]
        n_g = int(d["member"].sum())
        # 110%·90% 인원수는 반올림 (내림으로 하면 6월 OCI·지역난방공사·한국카본이 반대로 나옴 — 백테스트로 확인)
        keep_lim, new_lim = math.floor(n_g * 1.1 + 0.5), math.floor(n_g * 0.9 + 0.5)
        d["n_g"], d["keep_lim"], d["new_lim"] = n_g, keep_lim, new_lim
        if n_g < 3:
            d["sel"] = d["s1"] | (d["member"] & d["liq_ok"] & (d["g_rank"] <= max(keep_lim, n_g)))
        else:
            d["sel"] = ((d["member"] & d["liq_ok"] & ((d["g_rank"] <= keep_lim) | d["s1"]))
                        | (~d["member"] & d["s1"] & (d["g_rank"] <= new_lim)))
        out.append(d)
    e = pd.concat(out)
    e["stage"] = e["sel"].map({True: "1·2차", False: ""})

    # 3차: 200개로 맞춤 (산업군 무관)
    sel = e[e["sel"]]
    if len(sel) < size:
        pool = e[~e["sel"] & e["liq_ok"]].sort_values("avg_mcap", ascending=False).head(size - len(sel))
        e.loc[pool.index, ["sel", "stage"]] = [True, "3차 추가"]
    elif len(sel) > size:
        drop = sel.sort_values("avg_mcap").head(len(sel) - size)
        e.loc[drop.index, ["sel", "stage"]] = [False, "3차 제외"]
    # 대형주 특례: 15거래일 평균 시총 50위 이내 미선정 → 편입, 최소 시총 선정종목 제외
    e["rank15"] = e["mcap15"].rank(ascending=False, method="first")
    for i in e[(e["rank15"] <= 50) & ~e["sel"]].index:
        e.loc[i, ["sel", "stage"]] = [True, "대형주 특례"]
        smallest = e[e["sel"] & e["stage"].ne("대형주 특례")].sort_values("avg_mcap").index[0]
        e.loc[smallest, ["sel", "stage"]] = [False, "특례로 제외"]

    e["overall_rank"] = e["avg_mcap"].rank(ascending=False, method="first").astype(int)
    x = x.merge(e[["code", "g_rank", "n_g", "keep_lim", "new_lim", "liq_ok", "s1", "sel", "stage", "overall_rank"]],
                on="code", how="left")
    x["sel"] = x["sel"].astype("boolean").fillna(False).astype(bool)
    x["판정_raw"] = ""
    x.loc[~x["member"] & x["sel"], "판정_raw"] = "편입"
    x.loc[x["member"] & ~x["sel"], "판정_raw"] = "편출"
    x.loc[x["member"] & ~x["eligible"], "판정_raw"] = "편출(심사대상 제외)"
    x.attrs.update({"window": (start, end), "base": base, "n_sel": int(x["sel"].sum())})
    return x
