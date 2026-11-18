"""Nasdaq-100 · S&P 500 편입/편출 예상 스크린.

규칙 파라미터는 PARAMS 에 모아 두었고, 리포트 '방법론' 시트에 그대로 찍힌다.
예외(금융 분류 오류·해외 도미사일 등)는 data/overrides/us_overrides.csv 로 덮어쓴다.
"""
from __future__ import annotations

import datetime as dt
import re

import pandas as pd

from common import OVR, snap_dir, today, log, read_csv
from fetch_us_market import canon, gaap_quarters, public_float, px_ratio, us_returns

PARAMS = {
    "sp_mcap_min": 22.7e9,          # S&P 500 비조정 시총 하한 (S&P DJI 분기별 갱신 — 확인 시 수정)
    "sp_strong_mult": 1.5,          # 하한의 1.5배 이상 + 전 요건 충족 → '유력'
    "sp_drop_ratio": 0.35,          # 편입종목 시총이 하한의 35% 미만 → 편출 위험
    "sp_seasoning_months": 12,
    "ndx_size": 100,
    "ndx_auto": 75,                 # 상위 75위는 무조건 선정
    "ndx_buffer": 125,              # 기존 종목 101~125위는 조건부 유지(버퍼)
    "ndx_watch": 110,               # 비편입 101~110위는 순위 상승 시 후보
    "ndx_ref_date": "2026-11-30",   # 순위 기준일(11월 마지막 거래일) — 그 전까지는 현재 시총으로 추정
    "ndx_fast_entry_rank": 40,      # 신규상장이 기존 구성 상위 40위 이내면 Fast Entry
    "ndx_excl_sectors": ["Finance"],   # Real Estate 는 REIT 만 제외(아래 industry 필터)
}

BAD_SEC = re.compile(r"Preferred|%|Warrant|\bRights?\b|\bUnits?\b|Notes|Debenture|Series [A-Z]\b|"
                     r"Fund\b|ETF\b|Trust Preferred|Subordinated Deb", re.I)
# S&P 1500 부적격 형태: ADR, LP/MLP, BDC, 트래킹주식
SP_BAD = re.compile(r"American Depositary|\bADS\b|New York Registry|L\.P\.|\bLP\b|Limited Partnership|"
                    r"Capital Corp|BDC|Tracking Stock", re.I)


def _company_key(name: str) -> str:
    n = re.sub(r"\s+(Class|Series)\s+[A-Z]\b.*$", "", str(name))
    n = re.sub(r"\s+(Common Stock|Ordinary Shares|Capital Stock|Common Shares|American Depositary.*|"
               r"New York Registry.*|ADS|Depositary.*)$", "", n)
    return n.strip().lower()


def _security_ok(name: str) -> bool:
    if BAD_SEC.search(str(name)):
        return False
    if "Depositary" in str(name) and "American Depositary" not in str(name):
        return False
    return True


def _overrides() -> pd.DataFrame:
    p = OVR / "us_overrides.csv"
    if not p.exists():
        pd.DataFrame([
            # symbol, index(ndx|sp|all), action(exclude|include|note), reason
            ("TRI", "sp", "exclude", "캐나다 도미사일(TSX 주상장) — S&P 500 부적격"),
            ("SHOP", "sp", "exclude", "캐나다 도미사일"),
            ("TTD", "sp", "exclude", "2026-09-21 S&P 500 편출 — 1년간 재편입 심사 제외(~2027-09)"),
            ("POOL", "sp", "exclude", "2026-06-22 S&P 500 편출 — 재편입 제외(~2027-06)"),
            ("CPB", "sp", "exclude", "2026-06-22 S&P 500 편출 — 재편입 제외(~2027-06)"),
            ("SPCX", "sp", "note", "시즈닝 2027-06(IPO 2026-06-12 +12개월) + FY2025 GAAP 순손실 $4.9B + 유동주식 10% 미만 — 3중 미달. 2026-06-04 S&P 메가캡 완화안 부결로 예외 없음"),
            ("CBRS", "sp", "note", "2026년 상장 — 시즈닝 2027년, Fast track 없음"),
        ], columns=["symbol", "index", "action", "reason"]).to_csv(p, index=False, encoding="utf-8-sig")
    o = pd.read_csv(p, dtype=str).fillna("")
    o["symbol"] = o["symbol"].map(canon)
    return o


def _prep(u: pd.DataFrame) -> pd.DataFrame:
    u = u.copy()
    u["sec_ok"] = u["name"].map(_security_ok)
    u["ckey"] = u["name"].map(_company_key)
    return u


# ---------------- Nasdaq-100 ----------------
def screen_ndx(u: pd.DataFrame, ndx: pd.DataFrame) -> dict:
    P, o = PARAMS, _overrides()
    members = set(ndx["symbol"].map(canon))
    x = _prep(u[u["exchange"].eq("NASDAQ")])
    excl = set(o[(o["index"].isin(["ndx", "all"])) & o["action"].eq("exclude")]["symbol"])
    incl = set(o[(o["index"].isin(["ndx", "all"])) & o["action"].eq("include")]["symbol"])
    x["member"] = x["symbol"].isin(members)
    reit = x["industry"].fillna("").str.contains("Real Estate Investment Trust", case=False)
    x["eligible"] = (x["sec_ok"] & ~x["sector"].isin(P["ndx_excl_sectors"]) & ~reit & x["mcap"].notna()
                     & ~x["symbol"].isin(excl)) | x["symbol"].isin(incl) | x["member"]
    e = x[x["eligible"]].sort_values("mcap", ascending=False)
    # 회사 단위로 순위 (GOOG/GOOGL 등 복수 클래스는 1개 회사)
    comp = e.groupby("ckey", sort=False).agg(
        symbols=("symbol", lambda s: "/".join(s)), name=("name", "first"), mcap=("mcap", "max"),
        member=("member", "max"), sector=("sector", "first"), country=("country", "first"),
        ipoyear=("ipoyear", "max")).reset_index()
    comp = comp.sort_values("mcap", ascending=False).reset_index(drop=True)
    comp["rank"] = comp.index + 1
    n, buf, watch, auto = P["ndx_size"], P["ndx_buffer"], P["ndx_watch"], P["ndx_auto"]
    # 버퍼(101~125위 유지) 자격: 직전 재구성(2025-11-28) 때 100위 이내였거나 그 뒤 편입된 종목만.
    # 백테스트에서 BIIB(당시 112위)가 이 자격 미달로 실제 편출된 것을 확인 (2025-12 recon).
    from common import CACHE, load_json
    prev_rank = load_json(CACHE / "ndx_prev_recon_rank.json", default={}) or {}
    ADDED_SINCE = {"ALNY", "FER", "INSM", "MPWR", "STX", "WDC", "WMT", "SNDK", "LITE",
                   "ALAB", "CRWV", "NBIS", "RKLB", "TER", "HONA", "SPCX", "MRNA"}

    def buffer_ok(sym: str) -> str:
        if sym in ADDED_SINCE:
            return "O"
        r = prev_rank.get(sym)
        if r is None:
            return "?"          # 직전 순위 미상 — 자격 있다고 가정
        if r <= 100:
            return "O"
        if r <= 105:
            return "?"          # 근사 오차 범위 — 불확실
        return "X"
    # 해외 ADR(비편입): 본국 주상장(Non-Primary)이면 미국 상장 예탁주식분만으로 순위 → screener 시총은 과대.
    # 시뮬레이션에서는 빼고 '확인필요'로 따로 표시한다 (Primary ADR이면 us_overrides.csv 에 include).
    comp["adr_check"] = (~comp.member & comp["name"].str.contains("American Depositary|ADS|New York Registry", na=False)
                         & comp["country"].ne("United States") & ~comp["symbols"].isin(incl))
    sim = comp[~comp["adr_check"]].copy()
    sim["rank"] = range(1, len(sim) + 1)
    sim["buffer_q"] = [buffer_ok(s.split("/")[0]) for s in sim["symbols"]]
    chosen = _simulate_recon(sim, n=n, auto=auto, buf=buf)
    comp = comp.merge(sim[["ckey", "rank", "buffer_q"]].rename(columns={"rank": "sim_rank"}), on="ckey", how="left")
    comp["예상구성"] = comp["ckey"].isin(chosen)

    def verdict(r):
        sr = r.sim_rank
        if r.adr_check:
            return "편입 확인필요(해외 ADR — 미국상장분 시총 기준)" if r["rank"] <= watch else ""
        if r.member:
            if not r.예상구성:
                if pd.notna(sr) and n < sr <= buf and r.buffer_q == "X":
                    return "편출 유력(버퍼 자격 없음 — 직전 재구성 100위 밖)"
                return "편출 유력"
            if sr > n:
                tag = "·자격 불확실" if r.buffer_q == "?" else ""
                return f"편출 위험(101~125위 버퍼로 유지{tag})"
            return ""
        if r.예상구성:
            note = "(상장일 8/31 이전인지 확인)" if (pd.notna(r.ipoyear) and r.ipoyear >= today().year) else ""
            return "편입 유력" + note
        if sr <= n:
            return "편입 경합(버퍼 종목에 밀림)"
        if sr <= watch:
            return "편입 경합(순위 상승 시)"
        return ""

    comp["판정"] = [verdict(r) for _, r in comp.iterrows()]
    # Fast Entry: 비편입 신규상장이 기존 구성 상위 40위 시총 이상 (ADR 확인필요는 제외)
    mem_sorted = comp[comp.member].sort_values("mcap", ascending=False)
    fe_bar = mem_sorted["mcap"].iloc[P["ndx_fast_entry_rank"] - 1] if len(mem_sorted) >= P["ndx_fast_entry_rank"] else None
    recent = comp["ipoyear"].fillna(0) >= today().year - 1
    fe = ~comp.member & ~comp["adr_check"] & recent & (comp["mcap"] >= (fe_bar or 9e99))
    comp.loc[fe, "판정"] = "Fast Entry 대상(상장 15거래일 후)"
    comp["mcap_bn"] = (comp["mcap"] / 1e9).round(1)
    cutoff = sim.iloc[n - 1]["mcap"] if len(sim) >= n else None
    comp["vs_100위"] = (comp["mcap"] / cutoff - 1).round(3) if cutoff else None
    out = comp[(comp["rank"] <= 140) | comp["판정"].ne("")]
    n_add = int((~comp.member & comp["예상구성"]).sum())
    return {"table": out, "cut100": cutoff, "fe_bar": fe_bar, "n_members": int(comp.member.sum()), "n_add": n_add}


def _simulate_recon(sim: pd.DataFrame, n: int, auto: int, buf: int) -> set:
    """Nasdaq-100 연례 재구성 선정 순서 (방법론 2026-05):
    ① 상위 75위 ② 75~100위 기존종목 ③ 101~125위 기존종목(직전 재구성 때 100위 이내였거나 그 뒤 편입 — 현 구성종목은
    모두 해당한다고 가정) ④ 남은 자리에 100위 이내 비편입 순위순 — 100개에서 멈춤."""
    s = sim.sort_values("rank")
    bq = s["buffer_q"] if "buffer_q" in s else pd.Series("O", index=s.index)
    pick = list(s[s["rank"] <= auto]["ckey"])
    pick += list(s[s.member & s["rank"].between(auto + 1, n)]["ckey"])
    for k in s[s.member & s["rank"].between(n + 1, buf) & bq.ne("X")]["ckey"]:
        if len(pick) >= n:
            break
        pick.append(k)
    for k in s[~s.member & s["rank"].between(auto + 1, n)]["ckey"]:
        if len(pick) >= n:
            break
        pick.append(k)
    return set(pick[:n])


# ---------------- S&P 500 ----------------
def _seasoned(ipoyear) -> str:
    if pd.isna(ipoyear) or not ipoyear:
        return "OK"
    y = int(ipoyear)
    if y < today().year - 1:
        return "OK"
    if y == today().year:
        return "미달(올해 상장)"
    return "확인(작년 상장 — 상장일 기준 12개월)"


def screen_sp(u: pd.DataFrame, sp: pd.DataFrame, check_earnings: bool = True, exchanges=("NASDAQ",),
              sp400: pd.DataFrame | None = None) -> dict:
    P, o = PARAMS, _overrides()
    members = set(sp["symbol"].map(canon))
    excl = dict(zip(o[o["index"].isin(["sp", "all"]) & o["action"].eq("exclude")]["symbol"],
                    o[o["index"].isin(["sp", "all"]) & o["action"].eq("exclude")]["reason"]))
    x = _prep(u)
    x["member"] = x["symbol"].isin(members)
    # 회사 단위 멤버 여부 (한 클래스라도 편입이면 편입)
    mem_keys = set(x.loc[x["member"], "ckey"])
    x["member_co"] = x["ckey"].isin(mem_keys)

    # --- 편입 후보 ---
    c = x[~x["member_co"] & x["sec_ok"] & x["exchange"].isin(exchanges)
          & (x["mcap"] >= P["sp_mcap_min"])].drop_duplicates("ckey").copy()
    c["도미사일"] = c["country"].fillna("").map(lambda s: "미국" if s == "United States" else f"해외({s or '?'})")
    c.loc[c["name"].str.contains(SP_BAD, na=False), "도미사일"] = "부적격 형태(ADR·LP·BDC 등)"
    s400 = set(sp400["symbol"].map(canon)) if sp400 is not None else set()
    c["S&P400"] = c["symbol"].isin(s400).map({True: "소속(승격 후보)", False: ""})
    c.loc[c["symbol"].isin(excl), "도미사일"] = c["symbol"].map(excl)
    c["시즈닝"] = c["ipoyear"].map(_seasoned)
    notes = dict(zip(o[o["index"].isin(["sp", "all"]) & o["action"].eq("note")]["symbol"],
                     o[o["index"].isin(["sp", "all"]) & o["action"].eq("note")]["reason"]))
    c["메모"] = c["symbol"].map(notes).fillna("")
    rows = []
    for _, r in c.iterrows():
        g = gaap_quarters(r["symbol"]) if (check_earnings and r["도미사일"] == "미국") else {"err": "미조회"}
        if "err" in g:
            rows.append({"latest_q_ni": None, "ttm_ni": None, "ni_end": None, "흑자요건": g["err"]})
        else:
            ok = g["latest_q"] > 0 and g["ttm"] > 0
            rows.append({"latest_q_ni": g["latest_q"], "ttm_ni": g["ttm"], "ni_end": g["latest_end"],
                         "흑자요건": "충족" if ok else "미달"})
    c = pd.concat([c.reset_index(drop=True), pd.DataFrame(rows)], axis=1)
    # 유동시총(공시 float, 분기 지연) — 가격 변동으로 현재가 환산. 요건: ≥ 하한의 50%, IWF ≥ 0.10
    fl_rows = []
    for rec in c.itertuples(index=False):
        promising = rec.도미사일 == "미국" and rec.흑자요건 == "충족"
        pf = public_float(rec.symbol) if promising else {}
        if pf:
            ratio = px_ratio(rec.symbol, pf["float_asof"])
            fl_now = pf["float_usd"] / ratio if ratio else pf["float_usd"]
            iwf = fl_now / rec.mcap if rec.mcap else None
            ok = fl_now >= P["sp_mcap_min"] * 0.5 and (iwf or 0) >= 0.10
            note = "충족" if ok else "확인(공시 float 기준 미달 — 지연·태깅오류 가능)"
            if iwf and iwf > 1.5:
                note = "확인(공시값 이상 — 태깅오류 의심)"
            fl_rows.append({"float_bn": round(fl_now / 1e9, 1), "float_asof": pf["float_asof"],
                            "iwf_est": round(min(iwf, 1.5), 2) if iwf else None, "유동시총요건": note})
        else:
            fl_rows.append({"float_bn": None, "float_asof": "", "iwf_est": None,
                            "유동시총요건": "미조회" if not promising else "공시 없음"})
    c = pd.concat([c.reset_index(drop=True), pd.DataFrame(fl_rows)], axis=1)
    elig = c["도미사일"].eq("미국") & c["시즈닝"].eq("OK") & c["흑자요건"].eq("충족")
    c["판정"] = "요건 미달(관찰)"
    c.loc[elig, "판정"] = "편입 후보(요건 충족)"
    c.loc[elig & (c["mcap"] >= P["sp_mcap_min"] * P["sp_strong_mult"]), "판정"] = "편입 유력"
    c.loc[c["시즈닝"].str.startswith("확인") & c["도미사일"].eq("미국") & c["흑자요건"].eq("충족"), "판정"] = "편입 후보(상장일 확인)"
    need_chk = c["판정"].str.startswith("편입") & c["유동시총요건"].str.startswith("확인")
    c.loc[need_chk, "판정"] = c.loc[need_chk, "판정"].str.replace(r"\(.*\)$", "", regex=True).str.strip() + "(유동시총 확인)"
    c["mcap_bn"] = (c["mcap"] / 1e9).round(1)
    c["vs_하한"] = (c["mcap"] / P["sp_mcap_min"]).round(2)
    order = {"편입 유력": 0, "편입 후보(요건 충족)": 1, "편입 후보(상장일 확인)": 2, "요건 미달(관찰)": 3}
    c = c.sort_values(["판정", "mcap"], key=lambda s: s.map(order) if s.name == "판정" else -s).reset_index(drop=True)

    # --- 편출 위험: 편입 회사 중 시총 하위 ---
    m = x[x["member"]].sort_values("mcap", ascending=False).drop_duplicates("ckey")
    m = m.sort_values("mcap").head(25).copy()
    m["mcap_bn"] = (m["mcap"] / 1e9).round(1)
    m["vs_하한"] = (m["mcap"] / P["sp_mcap_min"]).round(2)
    m["판정"] = m["mcap"].map(lambda v: "편출 위험(하한 35% 미만)" if v < P["sp_mcap_min"] * P["sp_drop_ratio"]
                             else "하위권 관찰")
    return {"adds": c, "drops": m}


def run() -> dict:
    sd = snap_dir()
    u = read_csv(sd / "us_universe.csv")
    ndx = read_csv(sd / "const_ndx.csv")
    sp = read_csv(sd / "const_sp500.csv")
    r_ndx = screen_ndx(u, ndx)
    s4p = sd / "const_sp400.csv"
    r_sp = screen_sp(u, sp, sp400=read_csv(s4p) if s4p.exists() else None)
    # 수익률(1주/1개월/3개월) — 산출 테이블에 나오는 종목만 조회
    nt = r_ndx["table"].copy()
    nt["_sym"] = nt["symbols"].str.split("/").str[0]
    syms = set(nt.loc[nt["판정"].ne("") | (nt["rank"] <= 110), "_sym"])
    syms |= set(r_sp["adds"]["symbol"]) | set(r_sp["drops"]["symbol"])
    rets = us_returns(sorted(syms))
    r_ndx["table"] = nt.merge(rets, on="_sym", how="left").drop(columns="_sym")
    r_sp["adds"] = r_sp["adds"].merge(rets.rename(columns={"_sym": "symbol"}), on="symbol", how="left")
    r_sp["drops"] = r_sp["drops"].merge(rets.rename(columns={"_sym": "symbol"}), on="symbol", how="left")
    r_ndx["table"].to_csv(sd / "screen_ndx.csv", index=False, encoding="utf-8-sig")
    r_sp["adds"].to_csv(sd / "screen_sp_adds.csv", index=False, encoding="utf-8-sig")
    r_sp["drops"].to_csv(sd / "screen_sp_drops.csv", index=False, encoding="utf-8-sig")
    t = r_ndx["table"]
    log(f"[us] NDX 편입유력 {sum(t.판정.eq('편입 유력'))}, FastEntry {sum(t.판정.str.startswith('Fast'))}, "
        f"편출유력 {sum(t.판정.eq('편출 유력'))}, 버퍼위험 {sum(t.판정.str.startswith('편출 위험'))} | "
        f"S&P 후보 {sum(r_sp['adds'].판정.str.startswith('편입'))}")
    return {"ndx": r_ndx, "sp": r_sp}


if __name__ == "__main__":
    run()
