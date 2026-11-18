"""MSCI Japan (Standard) 편입·편출 예상 + 워크포워드 백테스트.

규칙: MSCI GIMI (선진시장) — 한국과 같은 구조, 유동성 기준만 DM 값.
  신규 편입 : 전체시총 ≥ 1.5C · 유동시총 ≥ 0.5C · 유동비율 ≥ 15% · 12개월 회전율 ≥ 20%
  기존 유지 : 전체시총 ≥ 2/3 C · 유동시총 ≥ 1/3 C · 회전율 ≥ 13.3%
  C(시장 기준선)는 비공개 → 과거 리뷰의 MSCI 공식 변경목록으로 역산(그리드 탐색).
워크포워드: 리뷰 r 의 C 는 '직전 리뷰에서 맞춘 C × 시장 규모 변화(TOPIX 달러 환산)'로 정한다
           → 결과를 모르는 상태에서 한 예측과 같다.

데이터
  구성종목  : iShares EWJ (MSCI Japan) 보유종목, 과거 날짜 조회 가능. 비중 0.02% 미만 잔여분 제외.
  공식 변경 : app2.msci.com/eqb/gimi/stdindex/MSCI_{Mon}{YY}_STPublicList.pdf → 'MSCI JAPAN INDEX' 블록
  유니버스  : yfinance 스크리너(일본, 시총 ¥1,500억↑) + 리뷰 당시 구성종목
  시총      : 과거 종가 × 현재 상장주식수(근사 — 자사주 소각 많은 일본은 과거 시총이 약간 과소)
  유동비율  : Yahoo floatShares / sharesOutstanding (MSCI FIF 와 정의 차이 있음)
"""
from __future__ import annotations

import datetime as dt
import difflib
import io
import re
import subprocess
import time
import warnings

import numpy as np
import pandas as pd

from common import CACHE, get, load_json, save_json, log, snap_dir, today, ymd

warnings.filterwarnings("ignore")
JP = CACHE / "msci_jp"
JP.mkdir(parents=True, exist_ok=True)

EWJ_PID = "239665"
EWJ_URL = ("https://www.blackrock.com/varnish-api/blk-one01-product-data/product-data/api/v1/get-fund-document"
           "?appType=PRODUCT_PAGE&appSubType=ISHARES&targetSite=us-ishares&locale=en_US&portfolioId={pid}"
           "&userType=individual&asOfDate={d}&component=holdings")
PDF_URL = "https://app2.msci.com/eqb/gimi/stdindex/MSCI_{tag}_STPublicList.pdf"
PDFTOTEXT = "pdftotext"

# 라벨, PDF 태그, 직전 EWJ 날짜, 직후 EWJ 날짜, 가격 판정구간(발표 전월 마지막 10영업일)
REVIEWS = [
    ("2025-11", "Nov25", "20251114", "20251205", "2025-10-17", "2025-10-31"),
    ("2026-02", "Feb26", "20260213", "20260306", "2026-01-16", "2026-01-30"),
    ("2026-05", "May26", "20260515", "20260605", "2026-04-17", "2026-04-30"),
    ("2026-08", "Aug26", "20260814", "20260904", "2026-07-17", "2026-07-31"),
]
NEXT = ("2026-11", "20261009", "2026-10-19", "2026-10-30")   # 다음 리뷰: 발표 11/11

P = {"add_mult": 1.5, "add_ff_mult": 0.5, "keep_mult": 2 / 3, "keep_ff_mult": 1 / 3,
     "min_fif": 0.15, "min_atvr": 0.20, "keep_atvr": 0.20 * 2 / 3, "band": 0.10, "min_weight": 0.02}
SCALE_TICKER = "1306.T"   # TOPIX 연동 ETF — 시장 규모 변화 척도


# ---------------------------------------------------------------- 구성종목·공식 변경
def ewj(d: str) -> pd.DataFrame:
    path = JP / f"ewj_{d}.csv"
    if path.exists():
        return pd.read_csv(path, dtype={"code": str})
    lines = get(EWJ_URL.format(pid=EWJ_PID, d=d)).text.splitlines()
    h = next(i for i, l in enumerate(lines) if l.startswith("Ticker,"))
    df = pd.read_csv(io.StringIO("\n".join(lines[h:])), dtype=str)
    df = df[df["Asset Class"].eq("Equity")]
    out = pd.DataFrame({"code": df["Ticker"].str.strip(), "name": df["Name"].str.strip(),
                        "weight": pd.to_numeric(df["Weight (%)"], errors="coerce")})
    out = out[out["code"].str.fullmatch(r"[0-9A-Z]{4}", na=False)]
    out.to_csv(path, index=False, encoding="utf-8-sig")
    return out


def members(d: str) -> pd.DataFrame:
    m = ewj(d)
    return m[m["weight"].fillna(0) >= P["min_weight"]]


def official(tag: str) -> tuple[list[str], list[str]]:
    """MSCI 공식 PDF 의 'MSCI JAPAN INDEX' 블록 → (편입 이름, 편출 이름)."""
    pdf = JP / f"msci_{tag}.pdf"
    if not pdf.exists():
        pdf.write_bytes(get(PDF_URL.format(tag=tag), timeout=60).content)
    txt = subprocess.run([PDFTOTEXT, "-layout", str(pdf), "-"], capture_output=True).stdout.decode("utf-8", "ignore")
    lines = txt.splitlines()
    i0 = next(i for i, l in enumerate(lines) if "MSCI JAPAN INDEX" in l)
    block = []
    for l in lines[i0 + 1:]:
        if re.search(r"MSCI [A-Z ]+ INDEX", l):
            break
        block.append(l)
    # 오른쪽(편출) 열 시작 위치: 'Additions' 행에서 두 번째 단어 시작점
    hdr = next(l for l in block if l.strip().startswith("Additions"))
    m = re.search(r"Additions\s{2,}(\S)", hdr)
    col = m.start(1) if m else 37
    adds, dels = [], []
    skip = re.compile(r"^(Additions|Deletions|None|Page \d+|MSCI Global Standard.*)$")
    for l in block:
        if "Global Standard Indexes" in l or re.search(r"Page \d+\s*$", l.strip()) and not l[col:].strip():
            if "Global Standard Indexes" in l:
                continue
        left, right = l[:col].strip(), l[col:].strip()
        for txt_, bucket in ((left, adds), (right, dels)):
            if txt_ and not skip.match(txt_):
                bucket.append(txt_)
    return adds, dels


def _norm(s: str) -> str:
    s = re.sub(r"[^A-Z0-9 ]", " ", str(s).upper())
    s = re.sub(r"\b(CO|LTD|INC|CORP|CORPORATION|HOLDINGS|HOLDING|GROUP|THE|KK)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def match_codes(names: list[str], ref: pd.DataFrame) -> dict[str, str | None]:
    """MSCI 표기명 → 종목코드. 정확일치 > (참조명이 더 긴) 접두일치 > 유사도 0.85.
    '짧은 참조명이 접두'인 경우는 쓰지 않는다 — MITSUI KINZOKU 가 MITSUI(미쓰이물산)로 붙는 오류 방지."""
    refn = {}
    for n, c in zip(ref["name"], ref["code"]):
        refn.setdefault(_norm(n), c)
    out = {}
    for n in names:
        k = _norm(n)
        hit = refn.get(k)
        if not hit:
            cands = sorted([rk for rk in refn if rk.startswith(k + " ")], key=len)
            if not cands:
                cands = difflib.get_close_matches(k, list(refn), n=1, cutoff=0.85)
            hit = refn[cands[0]] if cands else None
        out[n] = hit
    return out


def name_ref(*dates) -> pd.DataFrame:
    """매칭 참조표: EWJ(비중 필터 없이) 여러 날짜 + 일본 유니버스(Yahoo 영문명)."""
    parts = [ewj(d)[["code", "name"]] for d in dates]
    u = universe()
    parts.append(u[["code", "name"]])
    return pd.concat(parts).dropna().drop_duplicates(["code", "name"])


# ---------------------------------------------------------------- 시장 데이터
def universe() -> pd.DataFrame:
    path = JP / f"universe_{ymd(today())}.csv"
    if path.exists():
        return pd.read_csv(path, dtype={"code": str})
    import yfinance as yf
    from yfinance import EquityQuery
    q = EquityQuery("and", [EquityQuery("eq", ["region", "jp"]), EquityQuery("gt", ["intradaymarketcap", 1.5e11])])
    rows, off = [], 0
    while True:
        res = yf.screen(q, sortField="intradaymarketcap", sortAsc=False, size=250, offset=off)
        qs = res.get("quotes", [])
        rows += qs
        off += len(qs)
        if not qs or off >= res.get("total", 0):
            break
    df = pd.DataFrame([{"symbol": r["symbol"], "code": r["symbol"].split(".")[0], "name": r.get("longName") or r.get("shortName"),
                        "mcap": r.get("marketCap"), "shares": r.get("sharesOutstanding"),
                        "first_trade": r.get("firstTradeDateMilliseconds")} for r in rows if r["symbol"].endswith(".T")])
    df = df.drop_duplicates("code")
    df.to_csv(path, index=False, encoding="utf-8-sig")
    log(f"[msci-jp] 일본 유니버스 {len(df)}종목 (시총 ¥1,500억↑)")
    return df


def shares_float(symbols: list[str]) -> pd.DataFrame:
    """Yahoo 상장주식수·유동주식수 (주 1회 캐시)."""
    import yfinance as yf
    path = JP / "float.json"
    got = load_json(path, default={}) or {}
    fresh = path.exists() and (time.time() - path.stat().st_mtime) < 7 * 86400
    todo = [s for s in symbols if s not in got or not fresh]
    for i, s in enumerate(todo):
        try:
            inf = yf.Ticker(s).info
            got[s] = {"shares": inf.get("sharesOutstanding"), "float": inf.get("floatShares")}
        except Exception:
            got[s] = {"shares": None, "float": None}
        if i % 50 == 49:
            save_json(got, path)
    save_json(got, path)
    df = pd.DataFrame.from_dict(got, orient="index").rename_axis("symbol").reset_index()
    df["fif"] = (df["float"] / df["shares"]).clip(upper=1.0)
    return df


def prices(symbols: list[str], start: str = "2024-10-01") -> tuple[pd.DataFrame, pd.DataFrame]:
    """종가·거래량 패널 (일 단위 캐시)."""
    import yfinance as yf
    pc, vc = JP / f"close_{ymd(today())}.pkl", JP / f"vol_{ymd(today())}.pkl"
    close = pd.read_pickle(pc) if pc.exists() else pd.DataFrame()
    vol = pd.read_pickle(vc) if vc.exists() else pd.DataFrame()
    todo = [s for s in symbols if s not in close.columns]
    for i in range(0, len(todo), 80):
        chunk = todo[i:i + 80]
        d = yf.download(chunk, start=start, progress=False, auto_adjust=False, group_by="column", threads=True)
        if d.empty:
            continue
        c, v = d["Close"], d["Volume"]
        if isinstance(c, pd.Series):
            c, v = c.to_frame(chunk[0]), v.to_frame(chunk[0])
        close = pd.concat([close, c], axis=1)
        vol = pd.concat([vol, v], axis=1)
    close.index = pd.to_datetime(close.index).tz_localize(None)
    vol.index = pd.to_datetime(vol.index).tz_localize(None)
    close.to_pickle(pc)
    vol.to_pickle(vc)
    return close, vol


def fx_series(start="2024-10-01") -> pd.Series:
    import yfinance as yf
    s = yf.download("JPY=X", start=start, progress=False, auto_adjust=True)["Close"]
    s = s.iloc[:, 0] if isinstance(s, pd.DataFrame) else s
    s.index = pd.to_datetime(s.index).tz_localize(None)
    return s


# ---------------------------------------------------------------- 판정
def frame_at(win: tuple[str, str], mem: set[str], uni: pd.DataFrame, sf: pd.DataFrame,
             close: pd.DataFrame, vol: pd.DataFrame, fx: pd.Series) -> pd.DataFrame:
    s, e = pd.Timestamp(win[0]), pd.Timestamp(win[1])
    shares = dict(zip(sf["symbol"], sf["shares"]))
    fif = dict(zip(sf["symbol"], sf["fif"]))
    fx_w = float(fx[(fx.index >= s) & (fx.index <= e)].mean())
    rows = []
    for sym in close.columns:
        sh = shares.get(sym)
        if not sh:
            continue
        w = close.loc[(close.index >= s) & (close.index <= e), sym].dropna()
        if w.empty:
            continue
        full_jpy = float(w.mean()) * sh
        y = close.loc[(close.index > e - pd.Timedelta(days=365)) & (close.index <= e), sym]
        vv = vol.loc[y.index, sym]
        tv = float((y * vv).sum())
        f = fif.get(sym)
        f = f if f and not np.isnan(f) else 0.7
        ffm = full_jpy * f
        rows.append({"symbol": sym, "code": sym.split(".")[0], "full_usd": full_jpy / fx_w, "fif": f,
                     "ff_usd": ffm / fx_w, "atvr": tv / ffm if ffm else None, "member": sym.split(".")[0] in mem})
    df = pd.DataFrame(rows)
    nm = dict(zip(uni["code"], uni["name"]))
    df["name"] = df["code"].map(nm)
    return df


def classify(df: pd.DataFrame, C: float) -> pd.DataFrame:
    x = df.copy()
    add_f, add_ff = C * P["add_mult"], C * P["add_ff_mult"]
    keep_f, keep_ff = C * P["keep_mult"], C * P["keep_ff_mult"]
    band = P["band"]

    def v(r):
        if r.member:
            why = [w for w, bad in (("시총", r.full_usd < keep_f), ("유동시총", r.ff_usd < keep_ff),
                                    ("회전율", r.atvr is not None and r.atvr < P["keep_atvr"])) if bad]
            if why:
                return f"편출 유력({'/'.join(why)} 미달)"
            if r.full_usd < keep_f * (1 + band) or r.ff_usd < keep_ff * (1 + band):
                return "편출 위험(유지선 근접)"
            return ""
        block = [w for w, bad in (("유동비율", r.fif < P["min_fif"]),
                                  ("회전율", r.atvr is not None and r.atvr < P["min_atvr"])) if bad]
        ok = r.full_usd >= add_f and r.ff_usd >= add_ff
        if ok and not block:
            return "편입 유력"
        if ok:
            return f"편입 요건 미달({'/'.join(block)})"
        if r.full_usd >= add_f * (1 - band) and r.ff_usd >= add_ff * (1 - band) and not block:
            return "편입 경합(편입선 근접)"
        return ""

    x["판정"] = [v(r) for r in x.itertuples(index=False)]
    x["vs_add"] = x["full_usd"] / add_f
    x["vs_keep"] = x["full_usd"] / keep_f
    return x


COV_Q = 0.80   # 리뷰 후 구성 수 ≈ 유동시총 누적 80% 지점 순위 (2025-11~2026-08 실측: 165~174위 vs 167~182개)


def target_count(frame: pd.DataFrame, offset: float = 0.0) -> int:
    """목표 구성 수 N̂ = (전체시총 순 정렬 시) 유동시총 누적 80% 도달 순위 + 직전 리뷰 오차."""
    f = frame.sort_values("full_usd", ascending=False).reset_index(drop=True)
    cov = f["ff_usd"].cumsum() / f["ff_usd"].sum()
    return int((cov >= COV_Q).idxmax()) + 1 + int(round(offset))


def classify_v2(frame: pd.DataFrame, C: float, n_hat: int, watch: int = 8) -> pd.DataFrame:
    """GIMI 버퍼 규칙 + 종목수 조정: 기준선 C 아래 버퍼 구간의 기존 종목은 큰 순서로 n̂ 이 찰 때까지만 유지.
    필요 편출 수 k = 현 구성 + 편입 − n̂ 만큼, 하드 편출(유지선·유동시총 미달) 다음으로 버퍼 하위부터 편출."""
    x = classify(frame, C)
    n_now = int(x["member"].sum())
    n_add = int(x["판정"].eq("편입 유력").sum())
    hard = x["판정"].str.startswith("편출 유력")
    k = n_now + n_add - n_hat - int(hard.sum())
    buf = x[x["member"] & ~hard & (x["full_usd"] < C)].sort_values("full_usd")
    if k > 0:
        x.loc[buf.head(k).index, "판정"] = "편출 유력(종목수 조정 — 버퍼 하위)"
        nxt = buf.iloc[k:k + watch].index
    else:
        nxt = buf.head(watch).index
    for i in nxt:
        if x.at[i, "판정"] in ("", "편출 위험(유지선 근접)"):
            x.at[i, "판정"] = "편출 위험(종목수 경계)"
    x.attrs.update({"n_hat": n_hat, "k": k, "n_now": n_now, "n_add": n_add})
    return x


def score(x: pd.DataFrame, act_in: set, act_out: set) -> tuple[int, int, int, int]:
    p_in = set(x.loc[x["판정"].eq("편입 유력"), "code"])
    p_out = set(x.loc[x["판정"].str.startswith("편출 유력"), "code"])
    return len(p_in & act_in), len(p_out & act_out), len(p_in - act_in), len(p_out - act_out)


def best_C(frame: pd.DataFrame, act_in: set, act_out: set) -> tuple[float, tuple, list]:
    grid = np.exp(np.linspace(np.log(2e9), np.log(4e10), 160))
    res = []
    for C in grid:
        hi, ho, fi, fo = score(classify(frame, C), act_in, act_out)
        res.append((hi + ho - fi - fo, -(fi + fo), C, (hi, ho, fi, fo)))
    top = max(r[0] for r in res)
    ok = [r for r in res if r[0] == top]
    lo, hi_ = min(r[2] for r in ok), max(r[2] for r in ok)
    C = float(np.sqrt(lo * hi_))            # 최적 구간의 기하평균
    return C, score(classify(frame, C), act_in, act_out), [lo, hi_]


# ---------------------------------------------------------------- 실행
def load_all():
    uni = universe()
    mem_sets, names_ref = {}, []
    for lab, tag, pre, post, s, e in REVIEWS:
        mem_sets[pre], mem_sets[post] = members(pre), members(post)
    mem_sets[NEXT[1]] = members(NEXT[1])
    for m in mem_sets.values():
        names_ref.append(m)
    ref = pd.concat(names_ref).drop_duplicates("code")
    syms = sorted(set(uni["symbol"]) | {f"{c}.T" for c in ref["code"]})
    sf = shares_float(syms)
    close, vol = prices(syms)
    fx = fx_series()
    uni = pd.concat([uni, ref.assign(symbol=ref["code"] + ".T")[["symbol", "code", "name"]]]).drop_duplicates("code")
    return uni, mem_sets, ref, sf, close, vol, fx


def market_scale(close: pd.DataFrame, fx: pd.Series, win: tuple[str, str]) -> float:
    import yfinance as yf
    s = yf.download(SCALE_TICKER, start="2024-10-01", progress=False, auto_adjust=True)["Close"]
    s = s.iloc[:, 0] if isinstance(s, pd.DataFrame) else s
    s.index = pd.to_datetime(s.index).tz_localize(None)
    a, b = pd.Timestamp(win[0]), pd.Timestamp(win[1])
    px = float(s[(s.index >= a) & (s.index <= b)].mean())
    f = float(fx[(fx.index >= a) & (fx.index <= b)].mean())
    return px / f


def backtest(data=None) -> tuple[pd.DataFrame, dict]:
    uni, mem_sets, ref, sf, close, vol, fx = data or load_all()
    rows, calib = [], {}
    prev_C, prev_scale, prev_off = None, None, 0
    for lab, tag, pre, post, s, e in REVIEWS:
        adds, dels = official(tag)
        pre_m, post_m = mem_sets[pre], mem_sets[post]
        a_map = match_codes(adds, name_ref(post, NEXT[1]))
        d_map = match_codes(dels, name_ref(pre))
        act_in = {c for c in a_map.values() if c}
        act_out = {c for c in d_map.values() if c}
        frame = frame_at((s, e), set(pre_m["code"]), uni, sf, close, vol, fx)
        C_fit, sc_fit, rng = best_C(frame, act_in, act_out)
        scale = market_scale(close, fx, (s, e))
        n_cov = target_count(frame)
        n_post = len(post_m)
        if prev_C:
            C_wf = prev_C * scale / prev_scale
            x1 = classify(frame, C_wf)
            sc_v1 = score(x1, act_in, act_out)
            x = classify_v2(frame, C_wf, target_count(frame, prev_off))
            sc_wf = score(x, act_in, act_out)
            p_in = set(x.loc[x["판정"].eq("편입 유력"), "code"])
            p_out = set(x.loc[x["판정"].str.startswith("편출 유력"), "code"])
            risk = set(x.loc[x["판정"].str.startswith("편출 위험"), "code"])
        else:
            C_wf, sc_wf, sc_v1, p_in, p_out, risk = None, None, None, set(), set(), set()
        nm = {**dict(zip(ref["code"], ref["name"])), **dict(zip(uni["code"], uni["name"]))}
        f = lambda ss: ", ".join(sorted(str(nm.get(c, c))[:18] for c in ss))
        rows.append({
            "review": lab, "실제 편입": f(act_in), "실제 편출": f(act_out),
            "미매칭": ", ".join([n for n, c in {**a_map, **d_map}.items() if not c]),
            "사후 최적 C($B)": round(C_fit / 1e9, 2), "최적 구간($B)": f"{rng[0]/1e9:.1f}~{rng[1]/1e9:.1f}",
            "사후 적중(편입/편출/오탐입/오탐출)": sc_fit,
            "워크포워드 C($B)": round(C_wf / 1e9, 2) if C_wf else None,
            "워크포워드 편입 적중": f"{sc_wf[0]}/{len(act_in)}" if sc_wf else "—(첫 리뷰)",
            "워크포워드 편출 적중": f"{sc_wf[1]}/{len(act_out)}" if sc_wf else "—",
            "워크포워드 오탐(입/출)": f"{sc_wf[2]}/{sc_wf[3]}" if sc_wf else "—",
            "편출 위험권 포착": f"{len(risk & act_out)}건" if sc_wf else "—",
            "v1(종목수 조정 전) 편입/편출": f"{sc_v1[0]}/{len(act_in)} · {sc_v1[1]}/{len(act_out)}" if sc_v1 else "—",
            "목표종목수 추정/실제": f"{target_count(frame, prev_off) if prev_C else n_cov}/{n_post}",
            "예측 편입": f(p_in), "예측 편출": f(p_out),
        })
        calib[lab] = {"C": C_fit, "scale": scale, "range": rng, "cov_offset": n_post - n_cov}
        prev_C, prev_scale, prev_off = C_fit, scale, n_post - n_cov
        log(f"[msci-jp] {lab}: 실제 +{len(act_in)} −{len(act_out)} · 사후 C ${C_fit/1e9:.1f}B · "
            f"워크포워드 {'-' if not sc_wf else f'편입 {sc_wf[0]}/{len(act_in)} 편출 {sc_wf[1]}/{len(act_out)} 오탐 {sc_wf[2]}/{sc_wf[3]}'}")
    out = pd.DataFrame(rows)
    out.to_csv(snap_dir() / "bt_msci_jp.csv", index=False, encoding="utf-8-sig")
    save_json(calib, JP / "calib.json")
    return out, calib


def screen_now(data=None, calib=None) -> dict:
    """11월 리뷰 예상: 직전(8월) 리뷰 C × 시장 규모 변화. 판정 시총 = 최근 10영업일 평균."""
    uni, mem_sets, ref, sf, close, vol, fx = data or load_all()
    calib = calib or load_json(JP / "calib.json")
    last = calib[REVIEWS[-1][0]]
    end = close.dropna(how="all").index.max()
    start = close.index[close.index <= end][-10]
    win = (str(start.date()), str(end.date()))
    scale = market_scale(close, fx, win)
    C = last["C"] * scale / last["scale"]
    lo, hi = last["range"][0] * scale / last["scale"], last["range"][1] * scale / last["scale"]
    mem = members(NEXT[1])
    frame = frame_at(win, set(mem["code"]), uni, sf, close, vol, fx)
    n_hat = target_count(frame, last.get("cov_offset", 0))
    t = classify_v2(frame, C, n_hat)
    for tag, c in (("C하단", lo), ("C상단", hi)):
        t[f"판정({tag})"] = t["code"].map(classify_v2(frame, c, n_hat).set_index("code")["판정"]).fillna("")
    t.loc[t["판정"].eq("") & t["판정(C하단)"].eq("편입 유력"), "판정"] = "편입 경합(기준선 하단이면 편입)"
    t.loc[t["판정"].eq("") & t["판정(C상단)"].str.startswith("편출 유력"), "판정"] = "편출 위험(기준선 상단이면 편출)"
    nm = dict(zip(mem["code"], mem["name"]))
    t["name"] = t["name"].fillna(t["code"].map(nm))
    t["full_bn"], t["ff_bn"] = t["full_usd"] / 1e9, t["ff_usd"] / 1e9
    t = t.sort_values("full_usd", ascending=False).reset_index(drop=True)
    t["rank_jp"] = range(1, len(t) + 1)
    # 수익률
    for d in (7, 30, 91):
        t[f"ret_{d}d"] = [(_ret(close[s], d) if s in close else None) for s in t["symbol"]]
    out = t[(t["member"]) | t["판정"].ne("") | (t["rank_jp"] <= 260)]
    meta = {"C_bn": C / 1e9, "C_lo_bn": lo / 1e9, "C_hi_bn": hi / 1e9, "window": win,
            "add_bn": C * P["add_mult"] / 1e9, "add_ff_bn": C * P["add_ff_mult"] / 1e9,
            "keep_bn": C * P["keep_mult"] / 1e9, "keep_ff_bn": C * P["keep_ff_mult"] / 1e9,
            "n_members": len(mem), "fx": float(fx.iloc[-1]), "n_hat": n_hat, "k": t.attrs.get("k")}
    out.to_csv(snap_dir() / "screen_msci_jp.csv", index=False, encoding="utf-8-sig")
    log(f"[msci-jp] 11월 예상: C ${C/1e9:.1f}B · 편입유력 {sum(out.판정.eq('편입 유력'))} "
        f"편출유력 {sum(out.판정.str.startswith('편출 유력'))}")
    return {"table": out, "meta": meta}


def _ret(s: pd.Series, d: int):
    s = s.dropna()
    if len(s) < 2:
        return None
    w = s[s.index <= s.index[-1] - pd.Timedelta(days=d)]
    return float(s.iloc[-1] / w.iloc[-1] - 1) if len(w) else None


def run() -> dict:
    data = load_all()
    bt, calib = backtest(data)
    r = screen_now(data, calib)
    r["backtest"] = bt
    return r


if __name__ == "__main__":
    pd.set_option("display.width", 250)
    pd.set_option("display.max_colwidth", 300)
    r = run()
    print(r["backtest"].T.to_string())
    t = r["table"]
    print({k: (round(v, 2) if isinstance(v, float) else v) for k, v in r["meta"].items()})
    print(t[t["판정"].ne("")][["rank_jp", "code", "name", "member", "full_bn", "fif", "ff_bn", "vs_add", "vs_keep",
                               "atvr", "판정"]].round(2).to_string())
