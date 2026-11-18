"""KOSPI200 · MSCI Korea 편입/편출 예상 스크린.

파라미터(PARAMS)는 리포트 '방법론' 시트에 그대로 찍힌다. MSCI 기준선은 리뷰마다 바뀌므로
data/overrides/kr_params.csv 로 덮어쓸 수 있게 했다 (증권사 추정치가 나오면 거기에 넣는다).
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from common import OVR, snap_dir, today, log, read_csv
from kr_metrics import window_stats, float_table, price_frame
import events

PARAMS = {
    # --- KOSPI200 (방법론 확인 후 보정) ---
    "k2_size": 200,
    "k2_liq_pct": 0.85,          # 일평균 거래대금 상위 85% 이내만 심사
    "k2_in_rank": 160,           # 비구성종목: 일평균 시총 순위 이내면 '편입 유력'
    "k2_in_watch": 185,          #   ~ 이 순위까지 '편입 경합'
    "k2_out_rank": 250,          # 구성종목: 순위 밖이면 '편출 유력'
    "k2_out_watch": 225,         #   ~ 이 순위 밖이면 '편출 위험'
    # --- MSCI Korea Standard (GIMI 방법론 2026-08) — 기준선 C 는 원화(조원) ---
    "msci_cutoff_jo": 7.8,       # Standard 시장 기준선 C. 8월 증권사 추정 편입선 10.2~13조(=1.5C) → C 6.8~8.7조, 이후 시장 변화 반영
    "msci_cutoff_lo_jo": 6.9,    # 민감도: 기준선 하단
    "msci_cutoff_hi_jo": 8.8,    # 민감도: 기준선 상단
    "msci_add_mult": 1.5,        # Small Cap → Standard 편입: 전체시총 ≥ 1.5C
    "msci_add_ff_mult": 0.5,     #   유동시총 ≥ 0.5C
    "msci_keep_mult": 2 / 3,     # 기존 구성종목 유지: 전체시총 ≥ 2/3 C
    "msci_keep_ff_mult": 1 / 3,  #   유동시총 ≥ 1/3 C
    "msci_min_room": 0.15,       # 신규편입: 외국인 한도 여유 15% 이상
    "msci_keep_room": 0.0375,    # 기존: 여유 3.75% 미만이면 비중 0
    "msci_min_atvr": 0.15,       # 신규편입: 12개월 회전율 15% 이상 (기존 10%)
    "msci_keep_atvr": 0.10,
    "msci_member_min_weight": 0.02,  # EWY 비중 0.02% 미만 잔여 보유분(롯데쇼핑 1주 등)은 구성종목으로 보지 않음
    "msci_watch_band": 0.10,     # 기준 ±10% 이내는 '경합/위험'
}


FOL_DEFAULT = [  # 외국인 보유한도(%) — 법정·정관 한도. 틀리거나 빠진 종목은 kr_fol.csv 에서 고친다
    ("030200", "KT", 49), ("017670", "SK텔레콤", 49), ("032640", "LG유플러스", 49), ("053210", "KT스카이라이프", 49),
    ("015760", "한국전력", 40), ("051600", "한전KPS", 40), ("052690", "한전기술", 40), ("036460", "한국가스공사", 30),
    ("003490", "대한항공", 49), ("089590", "제주항공", 49), ("272450", "진에어", 49), ("091810", "티웨이항공", 49),
]


def _fol() -> dict[str, float]:
    p = OVR / "kr_fol.csv"
    if not p.exists():
        pd.DataFrame(FOL_DEFAULT, columns=["code", "name", "fol_pct"]).to_csv(p, index=False, encoding="utf-8-sig")
    d = pd.read_csv(p, dtype={"code": str})
    return dict(zip(d["code"], d["fol_pct"].astype(float)))


def _notes(index: str) -> dict[str, str]:
    """종목별 메모(과거 예외 등) — data/overrides/kr_notes.csv."""
    p = OVR / "kr_notes.csv"
    if not p.exists():
        pd.DataFrame([("MSCI Korea", "010130", "고려아연",
                       "2025-11~2026-08 4회 리뷰에서 시총·유동시총 요건을 크게 넘고도 미편입 — MSCI 개별 판단(사유 미확인), 편입 가능성 낮게 볼 것")],
                     columns=["index", "code", "name", "note"]).to_csv(p, index=False, encoding="utf-8-sig")
    d = pd.read_csv(p, dtype=str).fillna("")
    d = d[d["index"].eq(index)]
    return dict(zip(d["code"], d["note"]))


def _params() -> dict:
    P = dict(PARAMS)
    p = OVR / "kr_params.csv"
    if not p.exists():
        pd.DataFrame([(k, "" if v is None else v, "") for k, v in PARAMS.items()],
                     columns=["param", "value", "memo"]).to_csv(p, index=False, encoding="utf-8-sig")
    for r in pd.read_csv(p, dtype=str).fillna("").itertuples(index=False):
        if r.param in P and str(r.value).strip() != "":
            P[r.param] = float(r.value)
    return P


# ======================= MSCI Korea =======================
def msci_members(msci: pd.DataFrame, min_w: float) -> set[str]:
    return set(msci.loc[msci["weight"].fillna(0) >= min_w, "code"])


def msci_classify(pool: pd.DataFrame, members: set[str], P: dict, cutoff_krw: float) -> pd.DataFrame:
    """pool: code,name,market,mcap_krw(판정 시총),ff_est,atvr,foreign_room. GIMI 편입/유지 규칙 적용."""
    C = cutoff_krw
    add_full, add_ff = C * P["msci_add_mult"], C * P["msci_add_ff_mult"]
    keep_full, keep_ff = C * P["msci_keep_mult"], C * P["msci_keep_ff_mult"]
    band = P["msci_watch_band"]
    x = pool.copy()
    x["member"] = x["code"].isin(members)
    x["ff_mcap"] = x["mcap_krw"] * x["ff_est"].fillna(100) / 100

    def verdict(r):
        if r.member:
            why = []
            if r.mcap_krw < keep_full:
                why.append("시총")
            if r.ff_mcap < keep_ff:
                why.append("유동시총")
            if pd.notna(r.foreign_room) and r.foreign_room < P["msci_keep_room"]:
                why.append("외국인한도")
            if pd.notna(r.atvr) and r.atvr < P["msci_keep_atvr"]:
                why.append("회전율")
            if why:
                return f"편출 유력({'/'.join(why)} 미달)"
            if r.mcap_krw < keep_full * (1 + band) or r.ff_mcap < keep_ff * (1 + band):
                return "편출 위험(유지선 근접)"
            return ""
        block = []
        if pd.notna(r.foreign_room) and r.foreign_room < P["msci_min_room"]:
            block.append("외국인한도")
        if pd.notna(r.atvr) and r.atvr < P["msci_min_atvr"]:
            block.append("회전율")
        if pd.notna(r.ff_est) and r.ff_est < 15:
            block.append("유동비율<15%")
        size_ok = r.mcap_krw >= add_full and r.ff_mcap >= add_ff
        near = r.mcap_krw >= add_full * (1 - band) and r.ff_mcap >= add_ff * (1 - band)
        if size_ok and not block:
            return "편입 유력"
        if size_ok:
            return f"편입 요건 미달({'/'.join(block)})"
        if near and not block:
            why = "시총" if r.mcap_krw < add_full else "유동시총"
            return f"편입 경합({why} 편입선 근접)"
        if r.mcap_krw >= add_full and r.ff_mcap < add_ff:
            return "편입 요건 미달(유동시총)"
        return ""

    x["판정"] = [verdict(r) for r in x.itertuples(index=False)]
    x["vs_add"] = x["mcap_krw"] / add_full
    x["vs_keep"] = x["mcap_krw"] / keep_full
    x["ff_vs_add"] = x["ff_mcap"] / add_ff
    return x


def _liq_room(pool: pd.DataFrame, asof: dt.date) -> pd.DataFrame:
    fol = _fol()
    atvr, room = [], []
    for c, ffp, mc in zip(pool["code"], pool["ff_est"], pool["mcap_krw"]):
        df = price_frame(c)
        if df is None or df.empty:
            atvr.append(None); room.append(None); continue
        w = df[(df["date"] > pd.Timestamp(asof - dt.timedelta(days=365))) & (df["date"] <= pd.Timestamp(asof))]
        if w.empty:
            atvr.append(None); room.append(None); continue
        span = max(1, (w["date"].max() - w["date"].min()).days)
        tv = (w["close"] * w["volume"]).sum() * 365 / span
        ffm = mc * (ffp / 100 if pd.notna(ffp) else 1)
        atvr.append(tv / ffm if ffm else None)
        # 네이버 siseJson '외국인소진율' 칸은 실제로는 외국인 보유율(%) — 한도 종목은 (한도−보유)/한도
        fr = w["foreign_rate"].iloc[-1] if "foreign_rate" in w else None
        lim = fol.get(c, 100.0)
        room.append(max(0.0, (lim - fr) / lim) if fr is not None and pd.notna(fr) else None)
    pool = pool.copy()
    pool["atvr"], pool["foreign_room"] = atvr, room
    return pool


def screen_msci(u: pd.DataFrame, msci: pd.DataFrame) -> dict:
    P = _params()
    fx = float(msci["fx"].dropna().median()) if "fx" in msci and msci["fx"].notna().any() else 1400.0
    members = msci_members(msci, P["msci_member_min_weight"])
    x = u[u["kind"].eq("보통주")].copy()
    # 판정 시총: 최근 10영업일 평균 종가 × 주식수 (MSCI 는 10월 마지막 10영업일 중 임의 1일)
    m10 = []
    for c, sh in zip(x["code"], x["shares"]):
        df = price_frame(c)
        m10.append(df["close"].tail(10).mean() * sh if df is not None and len(df) else None)
    x["mcap_krw"] = pd.Series(m10, index=x.index).fillna(x["mcap_krw"])
    x = x.sort_values("mcap_krw", ascending=False)
    pool = pd.concat([x[x["code"].isin(members)], x[~x["code"].isin(members)].head(80)]).drop_duplicates("code")
    ff = float_table(pool["code"].tolist())
    pool = pool.merge(ff, on="code", how="left")
    pool = _liq_room(pool, today())
    C = P["msci_cutoff_jo"] * 1e12
    t = msci_classify(pool, members, P, C)
    for tag, key in (("C하단", "msci_cutoff_lo_jo"), ("C상단", "msci_cutoff_hi_jo")):
        alt = msci_classify(pool, members, P, P[key] * 1e12).set_index("code")["판정"]
        t[f"판정({tag})"] = t["code"].map(alt).fillna("")
    # 기준선 범위 어디서든 편입/편출로 나오면 최소 '경합/위험'으로 표시
    lo, hi = t["판정(C하단)"], t["판정(C상단)"]
    t.loc[t["판정"].eq("") & lo.str.startswith("편입 유력"), "판정"] = "편입 경합(기준선 하단이면 편입)"
    t.loc[t["판정"].eq("") & hi.str.startswith("편출 유력"), "판정"] = "편출 위험(기준선 상단이면 편출)"
    t["full_jo"] = t["mcap_krw"] / 1e12
    t["ff_jo"] = t["ff_mcap"] / 1e12
    t["full_usd"] = t["mcap_krw"] / fx
    t["ff_usd"] = t["ff_mcap"] / fx
    t = t.sort_values("mcap_krw", ascending=False).reset_index(drop=True)
    t["rank_kr"] = range(1, len(t) + 1)
    t["메모"] = t["code"].map(_notes("MSCI Korea")).fillna("")
    from kr_metrics import returns
    t = pd.concat([t, pd.DataFrame([returns(c) for c in t["code"]], index=t.index)], axis=1)
    meta = {"fx": fx, "cutoff_jo": P["msci_cutoff_jo"], "add_full_jo": C * P["msci_add_mult"] / 1e12,
            "add_ff_jo": C * P["msci_add_ff_mult"] / 1e12, "keep_jo": C * P["msci_keep_mult"] / 1e12,
            "keep_ff_jo": C * P["msci_keep_ff_mult"] / 1e12, "n_members": len(members),
            "asof_ewy": msci.attrs.get("asof", ""),
            # 하위 호환(리포트 제목용)
            "keep_usd": C * P["msci_keep_mult"] / fx, "add_full_usd": C * P["msci_add_mult"] / fx,
            "add_ff_usd": C * P["msci_add_ff_mult"] / fx, "src": "GIMI 규칙 × 기준선 C(kr_params.csv)"}
    return {"table": t, "meta": meta}


# ======================= KOSPI200 =======================
def _mgmt_flags(codes: list[str]) -> dict[str, str]:
    """네이버 basic: 관리종목·거래정지 여부 (심사대상 제외 사유)."""
    from common import get
    out = {}
    for c in codes:
        try:
            j = get(f"https://m.stock.naver.com/api/stock/{c}/basic", tries=2).json()
            if j.get("isManagement"):
                out[c] = "관리종목"
            elif j.get("tradableStatus") == "halt":
                out[c] = "거래정지"
        except Exception:
            pass
    return out


def screen_kospi200(u: pd.DataFrame, k2: pd.DataFrame, ref: dt.date | None = None) -> dict:
    """KRX 방법론 구현(kospi200_model). 12월 리뷰는 KRICS 9개 산업군(근사) 기준, 구 GICS 10개 기준도 병기."""
    import kospi200_model as K
    from fetch_industry import run as naver_industry
    ref = ref or events.next_ref_date("KOSPI200") or today()
    asof = min(ref, today())
    x = u[u["market"].eq("KOSPI") & u["kind"].eq("보통주")][["code", "name", "shares", "mcap_krw"]].copy()
    members = set(k2["code"])
    nav = naver_industry()
    ff = float_table(x.sort_values("mcap_krw", ascending=False).head(320)["code"].tolist())
    ffd = dict(zip(ff["code"], ff["ff_est"].fillna(100)))
    res = {}
    for scheme in ("KRICS9", "GICS10"):
        res[scheme] = K.select(x[["code", "name", "shares"]], members, K.group_map(scheme, nav), ref, asof=asof, ff=ffd)
    a = res["KRICS9"].copy()
    g = res["GICS10"].set_index("code")
    a["판정_GICS10"] = a["code"].map(g["판정_raw"]).fillna("")
    a["group_gics10"] = a["code"].map(g["group"])
    # 관리종목·거래정지 확인 (후보·구성종목 하위만)
    chk = a[(a["판정_raw"] != "") | (a["판정_GICS10"] != "") | (a["member"] & (a["overall_rank"].fillna(999) > 150))]
    flags = _mgmt_flags(chk["code"].tolist())
    a["상태"] = a["code"].map(flags).fillna("")

    def verdict(r):
        k, gg = r.판정_raw, r.판정_GICS10
        if r.상태:
            return f"편출 유력({r.상태})" if r.member else ""
        if k == "편입" and gg == "편입":
            return "편입 유력"
        if k == "편입":
            return "편입 경합(KRICS 분류 기준만)"
        if gg == "편입":
            return "편입 경합(구 GICS 분류 기준만)"
        if k.startswith("편출") and gg.startswith("편출"):
            return "편출 유력"
        if k.startswith("편출"):
            return "편출 위험(KRICS 분류 기준만)"
        if gg.startswith("편출"):
            return "편출 위험(구 GICS 분류 기준만)"
        # 경계: 구성종목인데 산업군 순위가 유지한도에 1 이내 / 비구성인데 1차선정 + 신규한도 바로 밖
        if r.member and pd.notna(r.g_rank) and r.g_rank >= r.keep_lim - 1 and not r.s1:
            return "편출 위험(경계)"
        if (not r.member) and r.s1 is True and pd.notna(r.g_rank) and r.g_rank <= r.new_lim + 2:
            return "편입 경합(경계)"
        return ""

    a["판정"] = [verdict(r) for r in a.itertuples(index=False)]
    from kr_metrics import returns
    rets = pd.DataFrame([returns(c) for c in a["code"]], index=a.index)
    a = pd.concat([a, rets], axis=1)
    a["avg_mcap_jo"] = a["avg_mcap"] / 1e12
    a["avg_tv_eok"] = a["avg_tv"] / 1e8
    a = a.sort_values("avg_mcap", ascending=False).reset_index(drop=True)
    a["rank"] = a["overall_rank"]
    start, end = res["KRICS9"].attrs["window"]
    meta = {"ref": ref, "window": f"{start} ~ {end}", "partial": end < ref, "n_members": len(members),
            "asof_pdf": k2.attrs.get("asof", ""), "scheme": "KRICS 9개(네이버 업종 근사 — KRX 공식 분류 10/26 공개 후 교체)",
            "official_krics": (OVR / "krics.csv").exists()}
    return {"table": a, "meta": meta}


# ======================= FTSE Korea (지역 밴드) =======================
def screen_ftse(u: pd.DataFrame, ftse: pd.DataFrame, fx: float = 1340.0,
                ff_map: dict | None = None) -> dict:
    """FTSE 규칙(지역 누적 시총 밴드)을 VEU+VSS 재구성 유니버스로 추정.
    편입선 = 지역 누적 86% 지점의 투자가능시총, 강등선 = 92% 지점. 시총 재순위는 3·9월 리뷰에서만."""
    import ftse_region
    members = set(ftse["code"])
    x = u[u["kind"].eq("보통주")].sort_values("mcap_krw", ascending=False).reset_index(drop=True)
    x["rank"] = range(1, len(x) + 1)
    x["member"] = x["code"].isin(members)
    ff_map = ff_map or {}
    th = ftse_region.thresholds(x[["code", "mcap_krw"]], ff_map, fx)
    add_l, drop_l = th["add_inv_usd"], th["drop_inv_usd"]
    x["ff_est"] = x["code"].map(ff_map)
    x["inv_usd"] = x["mcap_krw"] / fx * x["ff_est"].fillna(55) / 100
    x["vs_add"] = x["inv_usd"] / add_l
    x["vs_drop"] = x["inv_usd"] / drop_l
    fol = _fol()

    def room_of(c):
        df = price_frame(c)
        if df is None or "foreign_rate" not in df or not len(df):
            return None
        fr = df["foreign_rate"].iloc[-1]
        lim = fol.get(c, 100.0)
        return (lim - fr) / lim if fr is not None and pd.notna(fr) else None

    def v(r):
        if r.member:
            if r.vs_drop < 1:
                return "편출 후보(3월 리뷰)"
            if r.vs_drop < 1.15:
                return "편출 관찰(3월 리뷰)"
            return ""
        if r.vs_add >= 0.85:
            rm = room_of(r.code)
            if rm is not None and rm < 0.20:
                return "편입 불가(외국인한도 여유<20%)"
            return "편입 후보(3월 리뷰)" if r.vs_add >= 1 else "편입 관찰(3월 리뷰)"
        return ""

    x["판정"] = [v(r) for r in x.itertuples(index=False)]
    x["mcap_jo"] = x["mcap_krw"] / 1e12
    x["inv_usd_bn"] = x["inv_usd"] / 1e9
    from kr_metrics import returns
    sel_idx = x.index[x["member"] | x["판정"].ne("")]
    rr = pd.DataFrame([returns(x.at[i, "code"]) for i in sel_idx], index=sel_idx)
    x = pd.concat([x, rr], axis=1)
    out = x[(x["member"]) | x["판정"].ne("")]
    meta = {"n_members": len(members), "asof": ftse.attrs.get("asof", ""),
            "add_bn": add_l / 1e9, "drop_bn": drop_l / 1e9,
            "n_veu_kr": th.get("n_veu_kr"), "n_vss_kr": th.get("n_vss_kr")}
    return {"table": out, "meta": meta}


def step_safe(fn):
    try:
        return fn()
    except Exception as e:
        log(f"[kr] FTSE 스크린 실패: {type(e).__name__} {e}")
        return None


def run() -> dict:
    sd = snap_dir()
    u = read_csv(sd / "kr_universe.csv")
    k2 = read_csv(sd / "const_kospi200.csv")
    ms = read_csv(sd / "const_msci_korea.csv")
    ms.attrs["asof"] = (sd / "const_msci_korea.asof").read_text(encoding="utf-8")
    r_k2 = screen_kospi200(u, k2)
    r_ms = screen_msci(u, ms)
    ft_p = sd / "const_ftse_korea.csv"
    r_ft = None
    if ft_p.exists():
        ft = read_csv(ft_p)
        ft.attrs["asof"] = (sd / "const_ftse_korea.asof").read_text(encoding="utf-8") if (sd / "const_ftse_korea.asof").exists() else ""
        ff_map = dict(zip(r_ms["table"]["code"], r_ms["table"]["ff_est"])) if "msci" not in () and r_ms else {}
        r_ft = step_safe(lambda: screen_ftse(u, ft, fx=r_ms["meta"]["fx"], ff_map=ff_map))
        r_ft["table"].to_csv(sd / "screen_ftse.csv", index=False, encoding="utf-8-sig")
    r_k2["table"].to_csv(sd / "screen_kospi200.csv", index=False, encoding="utf-8-sig")
    r_ms["table"].to_csv(sd / "screen_msci.csv", index=False, encoding="utf-8-sig")
    for k, r in (("KOSPI200", r_k2), ("MSCI", r_ms)):
        t = r["table"]
        log(f"[kr] {k}: 편입유력 {sum(t.판정.eq('편입 유력'))} 경합 {sum(t.판정.str.contains('경합'))} "
            f"편출유력 {sum(t.판정.eq('편출 유력'))} 위험 {sum(t.판정.str.startswith('편출 위험'))}")
    out = {"kospi200": r_k2, "msci": r_ms}
    if r_ft:
        out["ftse"] = r_ft
    return out


if __name__ == "__main__":
    run()
