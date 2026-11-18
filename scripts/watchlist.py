"""지수 편입·편출 관심종목 워크북 (수익률 + 차트) — 트래커 판정에서 자동 선별, 등록 이력 관리.

선별 규칙 (트래커 판정 기준):
  KOSPI200   편입 유력 · 편출 유력
  MSCI Korea 편입 유력·경합 · 편출 유력·위험 (외국인한도 등 '요건 미달'은 제외)
  FTSE       편입 후보 · 편출 후보 (3월 리뷰)
  Nasdaq-100 편입 유력 · 편출 유력 · Fast Entry · 편입 경합 중 시뮬 순위 105위 이내
  S&P 500    편입 유력 · 편입 후보 시총 상위 10 · 편출 위험
등록 이력: data/watchlist_registry.csv — 처음 잡힌 날·가격을 남기고, 판정이 빠져도 '판정해제'로 보존.
고정(pin)=Y 로 두면 판정과 무관하게 계속 추적한다.

산출: output/관심종목_지수편입_YYYYMMDD.xlsx  (시트: 대시보드 · 관심종목 · 가격차트 · 수익률데이터 · 가격_KR · 가격_US · 등록이력)
"""
from __future__ import annotations

import datetime as dt
import re

import pandas as pd
from openpyxl.chart import BarChart, LineChart, Reference, Series
from openpyxl.chart.axis import DateAxis

import build_report as R
from common import DATA, OUT, CACHE, today, ymd, log, load_json, save_json
from kr_metrics import price_frame

REG = DATA / "watchlist_registry.csv"
REG_COLS = ["key", "지수", "그룹", "종목", "종목명", "시장", "등록일", "등록가", "최초판정", "현재판정",
            "상태", "해제일", "고정"]
BENCH = {"KOSPI": "^KS11", "KOSDAQ": "^KQ11", "S&P 500": "^GSPC", "Nasdaq-100": "^NDX", "TOPIX": "1306.T"}
WINDOW_DAYS = 91


# ---------------------------------------------------------------- 선별
def _short_us(name: str) -> str:
    n = re.sub(r"\s+(Class [A-Z].*|Common Stock.*|Ordinary Shares.*|American Depositary.*|Inc\.?|Corporation|"
               r"Corp\.?|Holdings?|Ltd\.?|plc|N\.V\.|Group)\b.*$", "", str(name))
    return n.strip()[:22] or str(name)[:22]


def select(kr: dict, us: dict) -> pd.DataFrame:
    rows = []

    def add(idx, grp, key, name, mkt, verdict, metric, bench):
        rows.append({"지수": idx, "그룹": grp, "종목": str(key), "종목명": name, "시장": mkt,
                     "판정": verdict, "핵심지표": metric, "벤치": bench})

    if "kospi200" in kr:
        t = kr["kospi200"]["table"]
        for r in t[t["판정"].eq("편입 유력")].itertuples():
            add("KOSPI200", "KOSPI200 편입 유력", r.code, r.name, "KR", r.판정,
                f"{r.group} {int(r.g_rank)}/{int(r.n_g)}위 · 6M평균 {r.avg_mcap_jo:.2f}조", "KOSPI")
        for r in t[t["판정"].str.startswith("편출 유력")].itertuples():
            add("KOSPI200", "KOSPI200 편출 유력", r.code, r.name, "KR", r.판정,
                f"{r.group} {int(r.g_rank) if pd.notna(r.g_rank) else '-'}/{int(r.n_g) if pd.notna(r.n_g) else '-'}위", "KOSPI")
    if "msci" in kr:
        t = kr["msci"]["table"]
        sel = t[t["판정"].str.match(r"^(편입 유력|편입 경합|편출 유력|편출 위험)")]
        for r in sel.itertuples():
            bench = "KOSDAQ" if r.market == "KOSDAQ" else "KOSPI"
            add("MSCI Korea", "MSCI Korea 후보", r.code, r.name, "KR", r.판정,
                f"시총 {r.full_jo:.1f}조 · 편입선 {r.vs_add:.2f}x · 유지선 {r.vs_keep:.2f}x", bench)
    if "msci_jp" in kr:
        t = kr["msci_jp"]["table"]
        sel = t[t["판정"].str.match(r"^(편입 유력|편입 경합|편출 유력|편출 위험)")]
        for r in sel.itertuples():
            grp = "MSCI Japan 편입 후보" if r.판정.startswith("편입") else "MSCI Japan 편출 후보"
            add("MSCI Japan", grp, r.symbol, str(r.name)[:22], "JP", r.판정,
                f"${r.full_bn:.1f}B · 편입선 {r.vs_add:.2f}x · 유지선 {r.vs_keep:.2f}x", "TOPIX")
    if "ftse" in kr:
        t = kr["ftse"]["table"]
        for grp, pat, asc in (("FTSE 승격 후보", "편입 후보", False), ("FTSE 강등 후보", "편출 후보", True)):
            sel = t[t["판정"].str.startswith(pat)].sort_values("inv_usd_bn", ascending=asc)
            for r in sel.itertuples():
                bench = "KOSDAQ" if r.market == "KOSDAQ" else "KOSPI"
                add("FTSE", grp, r.code, r.name, "KR", r.판정,
                    f"유동시총 ${r.inv_usd_bn:.1f}B · 편입선 {r.vs_add:.2f}x", bench)
    if "ndx" in us:
        t = us["ndx"]["table"]
        for r in t.itertuples():
            v = r.판정 or ""
            sym = r.symbols.split("/")[0]
            if v.startswith(("편입 유력", "Fast")):
                grp = "Nasdaq-100 편입"
            elif v.startswith("편출 유력"):
                grp = "Nasdaq-100 편출"
            elif v.startswith("편입 경합") and pd.notna(r.sim_rank) and r.sim_rank <= 105:
                grp = "Nasdaq-100 경합"
            else:
                continue
            add("Nasdaq-100", grp, sym, _short_us(r.name), "US", v,
                f"시뮬 {int(r.sim_rank) if pd.notna(r.sim_rank) else '-'}위 · ${r.mcap_bn:.1f}B", "Nasdaq-100")
    if "sp" in us:
        a = us["sp"]["adds"]
        a = a[a["판정"].str.startswith("편입")].sort_values("mcap", ascending=False)
        top = pd.concat([a[a["판정"].eq("편입 유력")], a[a["판정"].ne("편입 유력")].head(10)])
        for r in top.itertuples():
            add("S&P 500", "S&P 500 편입 후보", r.symbol, _short_us(r.name), "US", r.판정,
                f"${r.mcap_bn:.1f}B · 하한 {r.vs_하한:.2f}x", "S&P 500")
        d = us["sp"]["drops"]
        for r in d[d["판정"].str.startswith("편출")].itertuples():
            add("S&P 500", "S&P 500 편출 위험", r.symbol, _short_us(r.name), "US", r.판정,
                f"${r.mcap_bn:.1f}B · 하한 {r.vs_하한:.2f}x", "S&P 500")
    df = pd.DataFrame(rows)
    df["key"] = df["지수"] + "|" + df["종목"]
    return df.drop_duplicates("key")


# ---------------------------------------------------------------- 가격
def kr_panel(codes: list[str]) -> pd.DataFrame:
    import fetch_kr_market
    ser = {}
    for c in codes:
        df = price_frame(c)
        if df is None or df.empty:
            try:
                fetch_kr_market.history(c)
                df = price_frame(c)
            except Exception:
                df = None
        if df is not None and len(df):
            ser[c] = df.set_index("date")["close"].astype(float)
    return pd.DataFrame(ser).sort_index()


def yf_panel(tickers: list[str]) -> pd.DataFrame:
    import yfinance as yf
    import warnings
    warnings.filterwarnings("ignore")
    px = yf.download(sorted(set(tickers)), period="7mo", progress=False, auto_adjust=True)["Close"]
    if isinstance(px, pd.Series):
        px = px.to_frame(tickers[0])
    px.index = pd.to_datetime(px.index).tz_localize(None)
    # 장중 실행이면 한국 지수의 오늘 봉(미완성) 제거
    now = dt.datetime.now()
    if now.hour * 60 + now.minute < 15 * 60 + 40:
        for t in ("^KS11", "^KQ11"):
            if t in px:
                px.loc[px.index.date >= now.date(), t] = None
    return px


def ret(series: pd.Series, days: int) -> float | None:
    s = series.dropna()
    if len(s) < 2:
        return None
    w = s[s.index <= s.index[-1] - pd.Timedelta(days=days)]
    return float(s.iloc[-1] / w.iloc[-1] - 1) if len(w) else None


# ---------------------------------------------------------------- 등록 이력
def sync_registry(sel: pd.DataFrame, last_px: dict[str, float]) -> pd.DataFrame:
    reg = pd.read_csv(REG, dtype=str).fillna("") if REG.exists() else pd.DataFrame(columns=REG_COLS)
    now = str(today())
    cur = dict(zip(sel["key"], sel.to_dict("records")))
    out = []
    for r in reg.to_dict("records"):
        if r["key"] in cur:
            s = cur.pop(r["key"])
            r.update({"현재판정": s["판정"], "그룹": s["그룹"], "상태": "관찰중", "해제일": ""})
        elif r.get("고정") == "Y":
            r["상태"] = "관찰중(고정)"
        elif r["상태"].startswith("관찰중"):
            r.update({"상태": "판정해제", "해제일": now, "현재판정": ""})
        out.append(r)
    for k, s in cur.items():
        px = last_px.get(s["종목"])
        out.append({"key": k, "지수": s["지수"], "그룹": s["그룹"], "종목": s["종목"], "종목명": s["종목명"],
                    "시장": s["시장"], "등록일": now, "등록가": "" if px is None else f"{px:.4f}",
                    "최초판정": s["판정"], "현재판정": s["판정"], "상태": "관찰중", "해제일": "", "고정": ""})
    reg = pd.DataFrame(out, columns=REG_COLS)
    reg.to_csv(REG, index=False, encoding="utf-8-sig")
    return reg


# ---------------------------------------------------------------- 워크북
def build(kr: dict, us: dict, cal: pd.DataFrame) -> str:
    sel = select(kr, us)
    krp = kr_panel(sorted(sel.loc[sel["시장"].eq("KR"), "종목"].unique()))
    us_t = sorted(sel.loc[sel["시장"].isin(["US", "JP"]), "종목"].unique())
    yfp = yf_panel(us_t + list(BENCH.values()))
    for b in ("^KS11", "^KQ11"):
        if b in yfp:
            krp[b] = yfp[b].reindex(krp.index)
    usp = yfp[[c for c in yfp.columns if c not in ("^KS11", "^KQ11")]].dropna(how="all")
    panels = {"KR": krp, "US": usp, "JP": usp}

    last_px = {c: float(krp[c].dropna().iloc[-1]) for c in krp if krp[c].notna().any()}
    last_px.update({c: float(usp[c].dropna().iloc[-1]) for c in usp if usp[c].notna().any()})
    reg = sync_registry(sel, last_px)
    reg_map = dict(zip(reg["key"], reg.to_dict("records")))

    def series_of(mkt, code):
        p = panels[mkt]
        return p[code] if code in p else pd.Series(dtype=float)

    bench_sym = dict(BENCH)
    bench_ret = {}
    for bname, bsym in bench_sym.items():
        p = panels["KR"] if bsym.startswith("^K") else panels["US"]
        if bsym not in p:
            bench_ret[bname] = {7: None, 30: None, 91: None}
            continue
        s = p[bsym] if bsym in p else pd.Series(dtype=float)
        bench_ret[bname] = {d: ret(s, d) for d in (7, 30, 91)}

    recs = []
    for r in sel.itertuples(index=False):
        s = series_of(r.시장, r.종목)
        rr = {d: ret(s, d) for d in (7, 30, 91)}
        rg = reg_map.get(r.key, {})
        base = float(rg["등록가"]) if rg.get("등록가") else None
        last = float(s.dropna().iloc[-1]) if s.notna().any() else None
        ev = cal[(cal["index"] == r.지수) & (cal["kind"] == "발표") & (cal["D-day"] >= 0)]
        rb = cal[(cal["index"] == r.지수) & (cal["kind"] == "리밸런싱") & (cal["D-day"] >= 0)]
        recs.append({**r._asdict(), "현재가": last, "ret_7d": rr[7], "ret_30d": rr[30], "ret_91d": rr[91],
                     "bench_30d": bench_ret[r.벤치][30], "excess_30d": (rr[30] - bench_ret[r.벤치][30])
                     if rr[30] is not None and bench_ret[r.벤치][30] is not None else None,
                     "등록일": rg.get("등록일", ""), "since_reg": (last / base - 1) if base and last else None,
                     "발표일": ev["date"].iloc[0] if len(ev) else None, "D-day": int(ev["D-day"].iloc[0]) if len(ev) else None,
                     "리밸런싱일": rb["date"].iloc[0] if len(rb) else None})
    tab = pd.DataFrame(recs)

    wb = R.new_workbook()
    gen = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    kr_last = krp.dropna(how="all").index.max().date() if len(krp) else ""
    us_last = usp.dropna(how="all").index.max().date() if len(usp) else ""

    # ===== 대시보드 =====
    ws = wb.create_sheet("대시보드")
    R.title(ws, "지수 편입·편출 관심종목",
            f"생성 {gen} · 가격 기준 한국 {kr_last} / 미국 {us_last} 종가 · 판정은 지수편입 트래커 모델 추정 · "
            f"그룹 평균은 단순평균")
    grp_rows = []
    for g, d in tab.groupby("그룹", sort=False):
        b = d["벤치"].iloc[0]
        grp_rows.append({"그룹": g, "종목수": len(d), "ret_7d": d["ret_7d"].mean(), "ret_30d": d["ret_30d"].mean(),
                         "ret_91d": d["ret_91d"].mean(), "벤치": b, "b_30d": bench_ret[b][30], "b_91d": bench_ret[b][91],
                         "excess_30d": d["excess_30d"].mean(), "발표일": d["발표일"].iloc[0], "D-day": d["D-day"].iloc[0]})
    gdf = pd.DataFrame(grp_rows)
    row = R.section(ws, 4, "그룹별 평균 수익률 vs 벤치마크", 11)
    row = R.table(ws, row, gdf, [("그룹", "그룹", "text", 22), ("종목수", "종목수", "int", 7),
                                 ("1주", "ret_7d", "pct", 9), ("1개월", "ret_30d", "pct", 9), ("3개월", "ret_91d", "pct", 9),
                                 ("벤치", "벤치", "text", 11), ("벤치 1개월", "b_30d", "pct", 10), ("벤치 3개월", "b_91d", "pct", 10),
                                 ("1개월 초과", "excess_30d", "pct", 10), ("다음 발표", "발표일", "date", 11),
                                 ("D-day", "D-day", "int", 7)], verdict_col=None)
    chart_anchor_row = row + 1

    # ===== 관심종목 =====
    ws2 = wb.create_sheet("관심종목")
    R.title(ws2, "관심종목 전체", "선별 규칙은 '등록이력' 시트 하단 참고 · 등록 후 수익률 = 처음 리스트에 잡힌 날 종가 대비")
    cols = [("그룹", "그룹", "text", 20), ("판정", "판정", "text", 26), ("종목", "종목", "text", 9), ("종목명", "종목명", "text", 20),
            ("핵심지표", "핵심지표", "text", 34), ("현재가", "현재가", "num2", 11), ("1주", "ret_7d", "pct", 8),
            ("1개월", "ret_30d", "pct", 8), ("3개월", "ret_91d", "pct", 8), ("벤치 1개월", "bench_30d", "pct", 9),
            ("1개월 초과", "excess_30d", "pct", 9), ("등록일", "등록일", "text", 11), ("등록 후", "since_reg", "pct", 8),
            ("다음 발표", "발표일", "date", 11), ("D-day", "D-day", "int", 6), ("리밸런싱(종가)", "리밸런싱일", "date", 12)]
    r2 = 4
    for g, d in tab.groupby("그룹", sort=False):
        r2 = R.section(ws2, r2, f"{g} ({len(d)})", len(cols))
        r2 = R.table(ws2, r2, d, cols)
    ws2.freeze_panes = "A4"

    # ===== 수익률데이터 (막대차트 원본) + 대시보드 막대차트 =====
    ws3 = wb.create_sheet("수익률데이터")
    R.title(ws3, "그룹별 수익률 (차트 원본)", "각 블록 마지막 행 = 벤치마크")
    r3, bar_specs = 4, []
    for g, d in tab.groupby("그룹", sort=False):
        d = d.sort_values("ret_30d", ascending=False, na_position="last").head(12)
        b = d["벤치"].iloc[0]
        ws3.cell(row=r3, column=1, value=g).font = R.F_HDR
        hdr = r3 + 1
        for j, h in enumerate(["종목", "1개월", "3개월"], start=1):
            c = ws3.cell(row=hdr, column=j, value=h)
            c.font, c.fill = R.F_HDR, R.FILL_HDR
        rr = hdr + 1
        for x in d.itertuples(index=False):
            ws3.cell(row=rr, column=1, value=x.종목명 if x.시장 == "KR" else x.종목)
            for j, v in ((2, x.ret_30d), (3, x.ret_91d)):
                c = ws3.cell(row=rr, column=j, value=None if v is None or pd.isna(v) else float(v))
                c.number_format = "0.0%"
            rr += 1
        ws3.cell(row=rr, column=1, value=f"[벤치] {b}")
        for j, v in ((2, bench_ret[b][30]), (3, bench_ret[b][91])):
            c = ws3.cell(row=rr, column=j, value=v)
            c.number_format = "0.0%"
        bar_specs.append((g, hdr, rr))
        r3 = rr + 2
    ws3.column_dimensions["A"].width = 24

    for i, (g, hdr, last_row) in enumerate(bar_specs):
        ch = BarChart()
        ch.type = "bar"
        ch.title = f"{g} — 1개월·3개월 수익률"
        ch.style = 10
        data = Reference(ws3, min_col=2, max_col=3, min_row=hdr, max_row=last_row)
        cats = Reference(ws3, min_col=1, min_row=hdr + 1, max_row=last_row)
        ch.add_data(data, titles_from_data=True)
        ch.set_categories(cats)
        for srs, color in zip(ch.series, ("4472C4", "C0504D")):
            srs.invertIfNegative = False           # 기본값 True 면 음수 막대가 흰색으로 반전됨
            srs.graphicalProperties.solidFill = color
            srs.graphicalProperties.line.solidFill = color
        ch.y_axis.number_format = "0%"
        ch.y_axis.majorGridlines = None
        ch.x_axis.delete = False
        ch.y_axis.delete = False
        ch.x_axis.tickLblPos = "low"            # 종목명을 0선이 아니라 왼쪽 끝에 → 음수 막대와 안 겹침
        ch.x_axis.scaling.orientation = "maxMin"  # 1개월 수익률 상위가 위로
        ch.y_axis.crosses = "max"               # 뒤집은 뒤에도 값 축은 아래쪽
        ch.legend.position = "t"
        ch.legend.overlay = False
        ch.title.overlay = False
        ch.height = max(6.5, 0.55 * (last_row - hdr) + 3)
        ch.width = 15
        col = "A" if i % 2 == 0 else "H"
        ws.add_chart(ch, f"{col}{chart_anchor_row + (i // 2) * int(ch.height * 2.1)}")

    # ===== 가격 패널 (정규화 100) + 가격차트 =====
    norms: dict[str, pd.DataFrame] = {}

    def write_panel(name, panel, labels):
        wsx = wb.create_sheet(name)
        p = panel[panel.index >= panel.index.max() - pd.Timedelta(days=WINDOW_DAYS)].copy()
        base = p.apply(lambda s: s.dropna().iloc[0] if s.notna().any() else None)
        norm = p.divide(base) * 100
        wsx.cell(row=1, column=1, value="날짜").font = R.F_HDR
        colmap = {}
        for j, c in enumerate(norm.columns, start=2):
            wsx.cell(row=1, column=j, value=labels.get(c, c)).font = R.F_HDR
            colmap[c] = j
        for i, (d, vals) in enumerate(norm.iterrows(), start=2):
            cell = wsx.cell(row=i, column=1, value=d.date())
            cell.number_format = "yyyy-mm-dd"
            for c, v in vals.items():
                if pd.notna(v):
                    wsx.cell(row=i, column=colmap[c], value=round(float(v), 2))
        wsx.column_dimensions["A"].width = 11
        norms[{"가격_KR": "KR", "가격_해외": "US"}[name]] = norm
        return wsx, colmap, len(norm) + 1, norm.index.min()

    labels = {r.종목: (r.종목명 if r.시장 == "KR" else r.종목) for r in tab.itertuples()}
    labels.update({"^KS11": "KOSPI", "^KQ11": "KOSDAQ", "^GSPC": "S&P 500", "^NDX": "Nasdaq-100", "1306.T": "TOPIX"})
    ws_kr, cm_kr, n_kr, d0_kr = write_panel("가격_KR", krp, labels)
    ws_us, cm_us, n_us, d0_us = write_panel("가격_해외", usp, labels)

    wsc = wb.create_sheet("가격차트")
    R.title(wsc, "그룹별 주가 추이 (3개월, 시작일=100)", "굵은 회색 = 벤치마크 · 그룹당 최대 8종목(1개월 수익률 상위)")
    for i, (g, d) in enumerate(tab.groupby("그룹", sort=False)):
        mkt = d["시장"].iloc[0]
        wsx, cm, nrow, d0 = (ws_kr, cm_kr, n_kr, d0_kr) if mkt == "KR" else (ws_us, cm_us, n_us, d0_us)
        bsym = bench_sym[d["벤치"].iloc[0]]
        ch = LineChart()
        ch.title = g
        ch.height, ch.width = 7.5, 16
        ch.y_axis.number_format = "0"
        ch.title.overlay = False
        ch.y_axis.majorGridlines = None
        ch.x_axis = DateAxis(crossAx=100)
        ch.x_axis.number_format = "mm/dd"
        ch.x_axis.majorTimeUnit = "months"
        ch.x_axis.majorUnit = 1
        ch.y_axis.crossAx = 500
        ch.x_axis.delete = False
        ch.y_axis.delete = False
        ch.x_axis.scaling.min = (dt.date(d0.year, d0.month, 1) - dt.date(1899, 12, 30)).days
        ch.legend.position = "b"
        ch.legend.overlay = False
        cats = Reference(wsx, min_col=1, min_row=2, max_row=nrow)
        picks = d.sort_values("ret_30d", ascending=False, na_position="last").head(8)["종목"].tolist()
        nk = "KR" if mkt == "KR" else "US"
        normp = norms[nk][[c for c in picks + [bsym] if c in norms[nk]]]
        if normp.notna().any().any():   # y축을 데이터 범위에 맞춤 (0부터 그리면 선이 눌림)
            lo, hi = float(normp.min().min()), float(normp.max().max())
            ch.y_axis.scaling.min = max(0, (int(lo) // 10) * 10 - 10)
            ch.y_axis.scaling.max = (int(hi) // 10) * 10 + 20
        for code in picks + [bsym]:
            if code not in cm:
                continue
            s = Series(Reference(wsx, min_col=cm[code], min_row=1, max_row=nrow), title_from_data=True)
            s.smooth = False
            if code == bsym:
                s.graphicalProperties.line.solidFill = "7F7F7F"
                s.graphicalProperties.line.width = 38100
            else:
                s.graphicalProperties.line.width = 15875
            ch.series.append(s)
        ch.set_categories(cats)
        col = "A" if i % 2 == 0 else "K"
        wsc.add_chart(ch, f"{col}{4 + (i // 2) * 16}")

    # ===== 등록이력 =====
    ws4 = wb.create_sheet("등록이력")
    R.title(ws4, "등록 이력 (data/watchlist_registry.csv)", "판정이 빠지면 '판정해제'로 남김 · 고정=Y 면 계속 추적")
    r4 = R.table(ws4, 4, reg, [(c, c, "text", 14 if c not in ("key",) else 22) for c in REG_COLS if c != "key"],
                 verdict_col="현재판정")
    R.kv(ws4, r4 + 1, [
        ("선별 규칙", "KOSPI200 편입·편출 유력 / MSCI 편입 유력·경합·편출 유력·위험 / FTSE 편입·편출 후보 / "
                   "Nasdaq-100 편입·편출 유력·Fast Entry·경합(시뮬 105위 이내) / S&P 500 편입 유력 + 후보 시총 상위 10 + 편출 위험"),
        ("수익률", "달력일 기준 7·30·91일 전 종가 대비. 한국 장중 실행 시 오늘 봉 제외"),
        ("가격 출처", "한국: 네이버 일봉 캐시 / 미국·지수: yfinance(수정주가)"),
    ])

    path = OUT / f"관심종목_지수편입_{ymd(today())}.xlsx"
    R.finish(wb, path)
    log(f"[watchlist] {len(tab)}종목 · {tab['그룹'].nunique()}그룹 → {path.name}")
    return str(path)
