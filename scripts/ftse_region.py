"""FTSE All-World 한국 편입·편출 기준선 추정 (한국 앵커 방식).

이론(지역 누적 86%/92% 백분위)으로 계산하면 호주·대만의 높은 유동비율 때문에 기준선이 부풀어
한국 구성종목 절반이 편출 후보로 나온다(2026-10-12 실측). 대신 실제 지수 결과를 앵커로 쓴다:

  편입선 = VSS(소형지수)에 남아 있는 한국 종목 유동시총 상위 분포의 끝(p98)
           — 이보다 크면서도 승격 안 된 종목이 사실상 없는 수준
  편출선 = VEU(대·중형) 한국 구성종목 유동시총 하위 p05
           — 직전 리뷰를 통과한 종목들이 실존하는 하한

둘 다 '한국 종목의 투자가능(유동) 시총 USD' 단위라 후보 비교에 환산 왜곡이 없다.
우선주(코드 끝자리 ≠0)·펀드 잔여분(가치 $5M 미만)은 앵커 표본에서 제외.
"""
from __future__ import annotations

import pandas as pd

from common import CACHE, get, load_json, save_json, log, today, ymd

DEFAULT_FF = 55.0


def _fund_rows(fund: str) -> list[dict]:
    rows, start = [], 1
    for _ in range(40):
        j = get(f"https://investor.vanguard.com/vmf/api/{fund}/portfolio-holding/stock.json",
                params={"start": start, "count": 500},
                headers={"Accept": "application/json", "Referer": "https://investor.vanguard.com/"}).json()
        ent = (j.get("fund") or {}).get("entity") or []
        rows += ent
        if not j.get("next") or len(ent) < 500:
            break
        start += 500
    return rows


def _kr_inv(fund: str, mcap_usd: dict[str, float], ff_map: dict[str, float]) -> pd.DataFrame:
    """해당 펀드의 한국 보통주 라인 → 우리 데이터 기준 유동시총(USD)."""
    rows = _fund_rows(fund)
    kr = [r for r in rows if str(r.get("isin", "")).startswith("KR7")
          and float(r.get("marketValue") or 0) > 1e6]
    df = pd.DataFrame({"code": [str(r.get("ticker", "")).zfill(6) for r in kr],
                       "value": [float(r["marketValue"]) for r in kr],
                       "asof": [str(r.get("asOfDate", ""))[:10] for r in kr]})
    df = df[df["code"].str.fullmatch(r"\d{6}") & df["code"].str.endswith("0")]  # 보통주만
    df["full_usd"] = df["code"].map(mcap_usd)
    df["ff"] = df["code"].map(lambda c: ff_map.get(c) or DEFAULT_FF) / 100
    df["inv_usd"] = df["full_usd"] * df["ff"]
    df = df.dropna(subset=["inv_usd"])
    log(f"[ftse-region] {fund}: 한국 보통주 {len(df)}종목 (asof {df['asof'].iloc[0] if len(df) else '?'})")
    return df


def thresholds(kr_univ: pd.DataFrame, ff_map: dict[str, float], fx: float) -> dict:
    cache = CACHE / f"ftse_kr_anchor_{ymd(today())}.json"
    hit = load_json(cache)
    if hit:
        return hit
    mcap_usd = dict(zip(kr_univ["code"], kr_univ["mcap_krw"] / fx))
    veu = _kr_inv("VEU", mcap_usd, ff_map)   # All-World 한국 구성 (대·중형)
    vss = _kr_inv("VSS", mcap_usd, ff_map)   # Small Cap 한국 구성 (비 All-World)
    vss_only = vss[~vss["code"].isin(set(veu["code"]))]
    drop_line = float(veu["inv_usd"].quantile(0.05))
    add_line = max(float(vss_only["inv_usd"].quantile(0.95)), 2 * drop_line)
    out = {"add_inv_usd": add_line, "drop_inv_usd": drop_line,
           "n_veu_kr": int(len(veu)), "n_vss_kr": int(len(vss_only)),
           "asof_fund": veu["asof"].iloc[0] if len(veu) else "", "calc_date": str(today()), "fx": fx}
    save_json(out, cache)
    log(f"[ftse-region] 편입선 ${add_line / 1e9:.2f}B · 편출선 ${drop_line / 1e9:.2f}B (유동시총)")
    return out
