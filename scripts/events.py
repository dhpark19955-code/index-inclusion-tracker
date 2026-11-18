"""리뷰 일정표. data/overrides/calendar.csv 가 원본(사람이 고치는 값)이고, 없으면 기본값으로 만든다.

status: 확정(공식 공지) / 예상(관행·방법론상 추정) — 예상 일정은 공식 발표가 나오면 calendar.csv 를 고친다.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from common import OVR, today

COLS = ["index", "event", "date", "kst_note", "kind", "status", "source"]

DEFAULT = [
    # index, event, date, kst_note, kind(기준일|발표|리밸런싱|효력), status, source
    ("KOSPI200", "12월 정기변경 심사기준일(시총 판정 기간 종료)", "2026-10-30", "10월 마지막 매매일", "기준일", "예상", "KRX 방법론(관행)"),
    ("MSCI Korea", "11월 반기리뷰 시총 판정일(10월 마지막 10영업일 중 임의 1일)", "2026-10-19", "10/19~10/30 중 하루", "기준일", "예상", "MSCI 방법론"),
    ("Nasdaq-100", "연례 재구성 순위 기준일(11월 마지막 거래일)", "2026-11-30", "미국 11/30 종가", "기준일", "확정", "Nasdaq 방법론(2026-04)"),
    ("MSCI Korea", "11월 반기리뷰 발표", "2026-11-11", "한국시간 11/12(목) 오전 7시경", "발표", "확정", "MSCI index review dates"),
    ("KOSPI200", "12월 정기변경 발표", "2026-11-20", "11월 하순 장마감 후(6월분은 5/22 발표)", "발표", "예상", "관행"),
    ("FTSE", "12월 분기리뷰 발표", "2026-11-20", "11월 하순(미확정)", "발표", "예상", "FTSE Russell 관행"),
    ("MSCI Korea", "11월 반기리뷰 리밸런싱(종가)", "2026-11-30", "11/30 종가", "리밸런싱", "확정", "MSCI (효력 12/1)"),
    ("MSCI Korea", "11월 반기리뷰 효력", "2026-12-01", "", "효력", "확정", "MSCI index review dates"),
    ("S&P 500", "12월 분기 리밸런싱 발표", "2026-12-04", "미국 12/4(금) 장마감 후 → 한국 12/5 오전", "발표", "예상", "S&P DJI 관행(12월 첫 금요일)"),
    ("KOSPI200", "12월 정기변경 리밸런싱(종가)", "2026-12-10", "12월 선물만기일(둘째 목) 종가", "리밸런싱", "예상", "KRX 방법론"),
    ("KOSPI200", "12월 정기변경 효력", "2026-12-11", "만기일 다음 매매일", "효력", "예상", "KRX 방법론"),
    ("Nasdaq-100", "연례 재구성 발표", "2026-12-11", "미국 12/11(금) 장마감 후 → 한국 12/12 오전", "발표", "예상", "Nasdaq 관행(12월 둘째 금요일)"),
    ("S&P 500", "12월 분기 리밸런싱(종가)", "2026-12-18", "미국 12/18(금) 종가 = 한국 12/19 새벽", "리밸런싱", "예상", "셋째 금요일"),
    ("Nasdaq-100", "연례 재구성 리밸런싱(종가)", "2026-12-18", "미국 12/18(금) 종가", "리밸런싱", "예상", "셋째 금요일"),
    ("FTSE", "12월 분기리뷰 리밸런싱(종가)", "2026-12-18", "셋째 금요일 종가(미확정)", "리밸런싱", "예상", "FTSE Russell 관행"),
    ("S&P 500", "12월 분기 리밸런싱 효력", "2026-12-21", "개장 전", "효력", "예상", "S&P DJI"),
    ("Nasdaq-100", "연례 재구성 효력", "2026-12-21", "개장 전", "효력", "예상", "Nasdaq"),
]


def load() -> pd.DataFrame:
    p = OVR / "calendar.csv"
    if not p.exists():
        pd.DataFrame(DEFAULT, columns=COLS).to_csv(p, index=False, encoding="utf-8-sig")
    df = pd.read_csv(p, dtype=str).fillna("")
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["D-day"] = [(d - today()).days for d in df["date"]]
    return df.sort_values(["date", "index"]).reset_index(drop=True)


def upcoming(days: int = 60) -> pd.DataFrame:
    df = load()
    return df[(df["D-day"] >= 0) & (df["D-day"] <= days)]


def next_ref_date(index: str) -> dt.date | None:
    df = load()
    x = df[(df["index"] == index) & (df["kind"] == "기준일") & (df["D-day"] >= -45)]
    return x["date"].iloc[0] if len(x) else None
