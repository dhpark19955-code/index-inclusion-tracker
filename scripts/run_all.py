"""지수 편입·편출 예상 트래커 — 전체 실행.

  python run_all.py            # 수집 → 스크린 → 변경감지 → Excel + summary.json
  python run_all.py --no-fetch # 오늘 스냅샷으로 스크린·리포트만 다시

산출: output/지수편입_트래커_YYYYMMDD.xlsx, output/summary_YYYYMMDD.json, logs/run_YYYYMMDD.log
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import traceback
from pathlib import Path

import pandas as pd

import build_report as R
import diffs
import events
from common import OUT, OVR, snap_dir, today, ymd, log, read_csv

ERRORS: list[str] = []


def step(name, fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception as e:
        ERRORS.append(f"{name}: {type(e).__name__} {e}")
        log(f"[ERROR] {name}: {type(e).__name__} {e}\n{traceback.format_exc()}")
        return None


def broker_preds() -> pd.DataFrame:
    """증권사·언론 예상(수기 입력). 모델 판정 옆에 대조용으로 붙인다."""
    p = OVR / "broker_predictions.csv"
    if not p.exists():
        pd.DataFrame(columns=["지수", "종목", "종목명", "구분", "출처", "일자", "메모"]).to_csv(
            p, index=False, encoding="utf-8-sig")
    return pd.read_csv(p, dtype=str).fillna("")


def push_gsheet(file: str | None = None, key: str = "", title: str | None = None) -> str:
    """공개판에서는 Google Sheet 업로드를 하지 않는다(원본은 Drive API 로 같은 시트에 변환 업로드해 링크를 유지).
    GSHEET_PUSH_CMD 환경변수에 명령을 넣으면 그 명령 뒤에 xlsx 경로를 붙여 실행하고 마지막 출력 줄을 URL 로 쓴다."""
    cmd = os.environ.get("GSHEET_PUSH_CMD")
    if not cmd:
        return ""
    try:
        r = subprocess.run(cmd.split() + ([file] if file else []), capture_output=True, text=True,
                           encoding="utf-8", timeout=180)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip().splitlines()[-1]
        ERRORS.append(f"gsheet: {(r.stderr or r.stdout)[-200:]}")
    except Exception as e:  # noqa: BLE001
        ERRORS.append(f"gsheet: {type(e).__name__} {e}")
    return ""


def official_changes() -> pd.DataFrame:
    """공식 발표된 편입/편출(발표일에 예약작업 또는 사람이 입력). 예측 적중 대조에 쓴다."""
    p = OVR / "official_changes.csv"
    if not p.exists():
        pd.DataFrame(columns=["지수", "종목", "종목명", "구분", "발표일", "효력일", "출처"]).to_csv(
            p, index=False, encoding="utf-8-sig")
    return pd.read_csv(p, dtype=str).fillna("")


def hit_table(off: pd.DataFrame, pred: pd.DataFrame) -> pd.DataFrame:
    """공식 발표 vs 직전 예측: 종목별 적중 여부."""
    if off.empty:
        return pd.DataFrame(columns=["지수", "구분", "종목", "종목명", "모델 판정", "적중"])
    m = off.merge(pred[["지수", "종목", "판정"]], on=["지수", "종목"], how="left").fillna("")
    m["적중"] = [("O" if (g.startswith("편입") and p.startswith(("편입", "Fast"))) or (g.startswith("편출") and p.startswith("편출"))
                 else "X") for g, p in zip(m["구분"], m["판정"])]
    return m.rename(columns={"판정": "모델 판정"})[["지수", "구분", "종목", "종목명", "모델 판정", "적중", "발표일", "출처"]]


def main(fetch: bool = True):
    import fetch_constituents, fetch_kr_market, fetch_us_market, screen_kr, screen_us
    sd = snap_dir()
    log(f"===== run_all start (fetch={fetch}) =====")
    if fetch:
        step("constituents", fetch_constituents.run)
        need = set()
        for f in ("const_kospi200.csv", "const_msci_korea.csv"):
            if (sd / f).exists():
                need |= set(read_csv(sd / f)["code"])
        step("kr_market", fetch_kr_market.run, need_codes=need, top_kospi=0)
        step("us_market", fetch_us_market.run)

    kr = step("screen_kr", screen_kr.run) or {}
    us = step("screen_us", screen_us.run) or {}
    import msci_jp
    jp = step("msci_jp", msci_jp.run)
    if jp:
        kr["msci_jp"] = jp
    bp = broker_preds()

    # ---- 예측 통합 (변화 추적용) ----
    rows = []
    def add(idx, df, key, name, extra=lambda r: ""):
        if df is None:
            return
        for r in df[df["판정"].fillna("").ne("")].to_dict("records"):
            rows.append({"지수": idx, "종목": str(r[key]), "종목명": r[name], "판정": r["판정"]})
    if "kospi200" in kr:
        add("KOSPI200", kr["kospi200"]["table"], "code", "name")
    if "msci" in kr:
        add("MSCI Korea", kr["msci"]["table"], "code", "name")
    if "ftse" in kr:
        add("FTSE", kr["ftse"]["table"], "code", "name")
    if "ndx" in us:
        add("Nasdaq-100", us["ndx"]["table"], "symbols", "name")
    if "sp" in us:
        add("S&P 500", us["sp"]["adds"], "symbol", "name")
        add("S&P 500", us["sp"]["drops"][us["sp"]["drops"]["판정"].str.startswith("편출")], "symbol", "name")
    if "msci_jp" in kr:
        add("MSCI Japan", kr["msci_jp"]["table"], "code", "name")
    pred = pd.DataFrame(rows, columns=["지수", "종목", "종목명", "판정"])
    pchg = step("prediction_changes", diffs.prediction_changes, pred)
    diffs.save_predictions(pred)
    cchg = step("constituent_changes", diffs.constituent_changes)
    cal = events.load()
    off = official_changes()
    hits = hit_table(off, pred)

    path = OUT / f"지수편입_트래커_{ymd(today())}.xlsx"
    step("report", write_report, path, kr, us, bp, pred, pchg, cchg, cal, hits)

    summary = {
        "run_at": dt.datetime.now().isoformat(timespec="seconds"),
        "report": str(path),
        "errors": ERRORS,
        "counts": pred.groupby(["지수", "판정"]).size().reset_index(name="n").to_dict("records"),
        "prediction_changes": [] if pchg is None else pchg.head(40).to_dict("records"),
        "n_prediction_changes": 0 if pchg is None else len(pchg),
        "constituent_changes": [] if cchg is None else cchg.to_dict("records"),
        "official_hits": hits.to_dict("records"),
        "upcoming": [{**r, "date": str(r["date"])} for r in events.upcoming(21).to_dict("records")],
        "top": {idx: pred[(pred["지수"] == idx) & pred["판정"].str.match(r"^(편입 유력|편출 유력|Fast)")]
                .head(15).to_dict("records") for idx in pred["지수"].unique()},
    }
    summary["gsheet"] = push_gsheet()
    import watchlist
    wl = step("watchlist", watchlist.build, kr, us, cal)
    summary["watchlist_gsheet"] = push_gsheet(wl, key="watchlist", title="지수편입 관심종목 (Live)") if wl else ""
    (OUT / f"summary_{ymd(today())}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str),
                                                     encoding="utf-8")
    log(f"===== run_all done: report={path.name} errors={len(ERRORS)} pred_changes={summary['n_prediction_changes']} =====")
    return summary


# ------------------------------------------------------------------ report
def write_report(path, kr, us, bp, pred, pchg, cchg, cal, hits=None):
    import screen_kr, screen_us
    wb = R.new_workbook()
    gen = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    # ===== 요약 =====
    ws = wb.create_sheet("요약")
    R.title(ws, "지수 편입·편출 예상 트래커", f"생성 {gen} · 모델 추정치이며 공식 발표가 아님 · 초록=편입 / 빨강=편출 / 노랑=경합·확인")
    row = 4
    row = R.section(ws, row, "다가오는 일정 (60일)", 7)
    up = events.upcoming(60)
    row = R.table(ws, row, up, [("날짜", "date", "date", 12), ("D-day", "D-day", "int", 7), ("지수", "index", "text", 13),
                                 ("이벤트", "event", "text", 46), ("비고(한국시간 등)", "kst_note", "text", 34),
                                 ("상태", "status", "text", 8), ("출처", "source", "text", 22)], verdict_col=None)
    blocks = []
    if "kospi200" in kr:
        t = kr["kospi200"]["table"]
        t = t.assign(g_rank_txt=[f"{int(a)}/{int(b)}" if pd.notna(a) and pd.notna(b) else ""
                                 for a, b in zip(t["g_rank"], t["n_g"])])
        blocks.append(("KOSPI200 — 12월 정기변경", t, "code", "name",
                       [("산업군(KRICS 근사)", "group", "text"), ("산업군 순위/구성수", "g_rank_txt", "text"),
                        ("일평균시총(조)", "avg_mcap_jo", "num2"), ("1주", "ret_7d", "pct"), ("1개월", "ret_30d", "pct"),
                        ("3개월", "ret_91d", "pct")]))
    if "msci" in kr:
        t = kr["msci"]["table"]
        blocks.append(("MSCI Korea — 11월 리뷰", t, "code", "name",
                       [("시총(조)", "full_jo", "num2"), ("편입선 대비", "vs_add", "x"), ("유지선 대비", "vs_keep", "x"),
                        ("1개월", "ret_30d", "pct"), ("3개월", "ret_91d", "pct"), ("메모", "메모", "text")]))
    if "msci_jp" in kr:
        t = kr["msci_jp"]["table"]
        blocks.append(("MSCI Japan — 11월 리뷰", t, "code", "name",
                       [("시총($B)", "full_bn", "num1"), ("편입선 대비", "vs_add", "x"), ("유지선 대비", "vs_keep", "x"),
                        ("1개월", "ret_30d", "pct"), ("3개월", "ret_91d", "pct")]))
    if "ftse" in kr:
        t = kr["ftse"]["table"]
        sel_f = t[t["판정"].str.startswith(("편입 후보", "편출 후보"))]
        blocks.append((f"FTSE — 시총 재순위는 2027년 3월 · 상위 8건만 (전체 {len(sel_f)}건은 FTSE 시트)",
                       pd.concat([t[t["판정"].str.startswith("편입 후보")].sort_values("inv_usd_bn", ascending=False).head(4),
                                  t[t["판정"].str.startswith("편출 후보")].sort_values("inv_usd_bn").head(4)]), "code", "name",
                       [("유동시총($B)", "inv_usd_bn", "num1"), ("편입선 대비", "vs_add", "x"),
                        ("1개월", "ret_30d", "pct"), ("3개월", "ret_91d", "pct")]))
    if "ndx" in us:
        t = us["ndx"]["table"]
        blocks.append(("Nasdaq-100 — 12월 연례 재구성", t, "symbols", "name",
                       [("순위", "rank", "int"), ("시총($B)", "mcap_bn", "num1"),
                        ("1개월", "ret_30d", "pct"), ("3개월", "ret_91d", "pct")]))
    if "sp" in us:
        t = pd.concat([us["sp"]["adds"], us["sp"]["drops"]], ignore_index=True)
        blocks.append(("S&P 500 — 12월 분기 리밸런싱 (편입 후보=나스닥 상장)", t, "symbol", "name",
                       [("시총($B)", "mcap_bn", "num1"), ("하한 대비", "vs_하한", "x"),
                        ("1개월", "ret_30d", "pct"), ("3개월", "ret_91d", "pct")]))
    for ttl, t, key, nm, metric in blocks:
        row = R.section(ws, row, ttl, 7)
        sel = t[t["판정"].fillna("").str.match(r"^(편입|편출|Fast)")].copy()
        order = sel["판정"].map(lambda s: 0 if s.startswith(("Fast", "편입 유력")) else 1 if s.startswith("편입")
                               else 2 if s.startswith("편출 유력") else 3)
        sel = sel.assign(_o=order).sort_values(["_o"]).drop(columns="_o")
        idx_name = ttl.split(" —")[0].split(" ")[0] if ttl.startswith("FTSE") else ttl.split(" —")[0]
        bset = bp[bp["지수"].eq(idx_name)]
        sel["증권사예상"] = sel[key].astype(str).map(
            lambda k: ", ".join(f"{r.구분}({r.출처})" for r in bset[bset["종목"].eq(k)].itertuples()) or "")
        cols = [("판정", "판정", "text", 26), ("종목", key, "text", 13), ("종목명", nm, "text", 30)]
        cols += [(h, f, fmt, 13) for h, f, fmt in metric] + [("증권사·언론 예상", "증권사예상", "text", 30)]
        row = R.table(ws, row, sel.head(30), cols)
    if hits is not None and not hits.empty:
        row = R.section(ws, row, "공식 발표 vs 모델 예측 (적중 대조)", 7)
        row = R.table(ws, row, hits, [("지수", "지수", "text", 13), ("구분", "구분", "text", 12), ("종목", "종목", "text", 13),
                                       ("종목명", "종목명", "text", 30), ("모델 판정", "모델 판정", "text", 26),
                                       ("적중", "적중", "text", 6), ("발표일", "발표일", "text", 11)], verdict_col="구분")
    row = R.section(ws, row, "예측 변화 (직전 실행 대비)", 7)
    row = R.table(ws, row, pchg, [("지수", "지수", "text", 13), ("종목", "종목", "text", 13), ("종목명", "종목명", "text", 30),
                                   ("이전", "이전", "text", 26), ("현재", "현재", "text", 26), ("비교기준", "비교기준", "text", 10)],
                  verdict_col="현재", empty_msg="변화 없음 (또는 첫 실행)")
    row = R.section(ws, row, "실제 구성종목 변경 감지 (ETF 보유종목 기준)", 7)
    row = R.table(ws, row, cchg, [("지수", "지수", "text", 13), ("구분", "구분", "text", 16), ("종목", "종목", "text", 13),
                                   ("종목명", "종목명", "text", 30), ("비교기준", "비교기준", "text", 10)],
                  verdict_col="구분", empty_msg="변경 없음")
    ws.freeze_panes = "A4"

    # ===== 일정 =====
    ws = wb.create_sheet("일정")
    R.title(ws, "리뷰 일정", "data/overrides/calendar.csv 가 원본 — 공식 발표로 확정되면 그 파일의 status·date 를 고친다")
    R.table(ws, 4, cal, [("날짜", "date", "date", 12), ("D-day", "D-day", "int", 7), ("지수", "index", "text", 13),
                         ("이벤트", "event", "text", 50), ("비고", "kst_note", "text", 36), ("구분", "kind", "text", 9),
                         ("상태", "status", "text", 8), ("출처", "source", "text", 26)], verdict_col=None)
    ws.freeze_panes = "A5"

    # ===== KOSPI200 =====
    if "kospi200" in kr:
        m, t = kr["kospi200"]["meta"], kr["kospi200"]["table"].copy()
        ws = wb.create_sheet("KOSPI200")
        part = " (진행 중)" if m["partial"] else ""
        offi = " — KRX 공식 KRICS 적용됨" if m.get("official_krics") else ""
        R.title(ws, "KOSPI200 — 12월 정기변경 예상 (KRX 방법론 §6 구현)",
                f"심사기준일 {m['ref']} · 집계구간 {m['window']}{part} · 산업군 {m['scheme']}{offi} · "
                f"구성종목 {m['n_members']} · 백테스트 2026-06: 편입 4/4·편출 4/4(오탐 1) / 2025-12: 편입 7/10·편출 6/9")
        cols = [("판정", "판정", "text", 30), ("전체순위", "rank", "int", 8), ("종목코드", "code", "text", 9),
                ("종목명", "name", "text", 22), ("편입", "member", "text", 6), ("산업군(KRICS 근사)", "group", "text", 14),
                ("산업군 순위", "g_rank", "int", 9), ("산업군 구성수", "n_g", "int", 9), ("유지한도(110%)", "keep_lim", "int", 10),
                ("신규한도(90%)", "new_lim", "int", 10), ("1차(누적85%)", "s1", "text", 10), ("선정단계", "stage", "text", 10),
                ("일평균시총(조)", "avg_mcap_jo", "num2", 12), ("일평균거래대금(억)", "avg_tv_eok", "num1", 13),
                ("1주 수익률", "ret_7d", "pct", 9), ("1개월", "ret_30d", "pct", 9), ("3개월", "ret_91d", "pct", 9),
                ("구 GICS 기준", "판정_GICS10", "text", 12), ("구 GICS 산업군", "group_gics10", "text", 14), ("상태", "상태", "text", 9)]
        r = R.section(ws, 4, "편입 예상", len(cols))
        r = R.table(ws, r, t[t["판정"].str.startswith("편입")], cols)
        r = R.section(ws, r, "편출 예상", len(cols))
        r = R.table(ws, r, t[t["판정"].str.startswith("편출")], cols)
        r = R.section(ws, r, "전체 순위 (일평균시총 상위 300)", len(cols))
        R.table(ws, r, t.head(300), cols)

    # ===== MSCI =====
    if "msci" in kr:
        m, t = kr["msci"]["meta"], kr["msci"]["table"]
        ws = wb.create_sheet("MSCI Korea")
        R.title(ws, "MSCI Korea (Standard) — 11월 리뷰 예상 (GIMI 규칙)",
                f"기준선 C {m['cutoff_jo']:.1f}조 → 편입: 시총 ≥{m['add_full_jo']:.1f}조·유동 ≥{m['add_ff_jo']:.1f}조 / "
                f"유지: 시총 ≥{m['keep_jo']:.1f}조·유동 ≥{m['keep_ff_jo']:.1f}조 · 구성 {m['n_members']}종목 · "
                f"백테스트 2026-08: 편입 1/1·편출 4/4 / 2025-11: 편입 1/2·편출 1/1 · C 는 kr_params.csv 에서 수정")
        cols = [("판정", "판정", "text", 30), ("국내순위", "rank_kr", "int", 8), ("종목코드", "code", "text", 9),
                ("종목명", "name", "text", 18), ("시장", "market", "text", 7), ("편입", "member", "text", 6),
                ("시총(조)", "full_jo", "num2", 9), ("유동비율 추정(%)", "ff_est", "num1", 11), ("유동시총(조)", "ff_jo", "num2", 10),
                ("편입선 대비", "vs_add", "x", 9), ("유동 편입선 대비", "ff_vs_add", "x", 10), ("유지선 대비", "vs_keep", "x", 9),
                ("회전율(12M)", "atvr", "pct", 9), ("외국인 여유", "foreign_room", "pct", 9),
                ("1주 수익률", "ret_7d", "pct", 9), ("1개월", "ret_30d", "pct", 9), ("3개월", "ret_91d", "pct", 9),
                ("C 하단 시", "판정(C하단)", "text", 16), ("C 상단 시", "판정(C상단)", "text", 16), ("메모", "메모", "text", 40)]
        r = R.section(ws, 4, "편입 예상", len(cols))
        r = R.table(ws, r, t[t["판정"].str.startswith("편입")], cols)
        r = R.section(ws, r, "편출 예상", len(cols))
        r = R.table(ws, r, t[t["판정"].str.startswith("편출")], cols)
        r = R.section(ws, r, "구성종목 + 비구성 상위 80 (판정 시총 순)", len(cols))
        R.table(ws, r, t, cols)

    # ===== FTSE =====
    ws = wb.create_sheet("FTSE")
    if "ftse" in kr:
        m, t = kr["ftse"]["meta"], kr["ftse"]["table"]
        warn = " ⚠ 9월 반기리뷰(9/21 효력) 미반영 스냅샷" if str(m["asof"]) < "2026-09-21" else ""
        R.title(ws, "FTSE All-World Korea — 한국 앵커 기준선",
                f"구성 {m['n_members']}종목 (Vanguard VEU {m['asof']} 월말{warn}) · 12월 분기리뷰(11/13 발표)는 시총 재순위 없음 → "
                f"시총 판정은 2027년 3월(12월 말 데이터) · 편입선 유동시총 ${m['add_bn']:.2f}B(미승격 소형 상위 5% 경계) · "
                f"편출선 ${m['drop_bn']:.2f}B(현 구성 하위 5% 경계) — 경험적 앵커이며 FTSE 공식 백분위가 아님")
        cols = [("판정", "판정", "text", 26), ("국내 시총순위", "rank", "int", 10), ("종목코드", "code", "text", 9),
                ("종목명", "name", "text", 22), ("시장", "market", "text", 7), ("편입", "member", "text", 6),
                ("시총(조)", "mcap_jo", "num2", 10), ("유동비율(%)", "ff_est", "num1", 10),
                ("유동시총($B)", "inv_usd_bn", "num1", 11), ("편입선 대비", "vs_add", "x", 10), ("편출선 대비", "vs_drop", "x", 10),
                ("1주 수익률", "ret_7d", "pct", 9), ("1개월", "ret_30d", "pct", 9), ("3개월", "ret_91d", "pct", 9)]
        r = R.section(ws, 4, "편입·편출 후보 (3월 리뷰, 근사)", len(cols))
        r = R.table(ws, r, t[t["판정"].ne("")], cols)
        r = R.section(ws, r, "일정", len(cols))
        R.table(ws, r, cal[cal["index"].eq("FTSE")], [("날짜", "date", "date", 12), ("D-day", "D-day", "int", 10),
                                                      ("이벤트", "event", "text", 40), ("비고", "kst_note", "text", 30),
                                                      ("상태", "status", "text", 8)], verdict_col=None)
    else:
        R.title(ws, "FTSE Korea", "VEU 보유종목 수집 실패 — 다음 실행 때 재시도")

    # ===== NDX =====
    if "ndx" in us:
        t = us["ndx"]["table"]
        ws = wb.create_sheet("Nasdaq-100")
        R.title(ws, "Nasdaq-100 — 12월 연례 재구성 예상",
                f"순위 기준일 {screen_us.PARAMS['ndx_ref_date']}(그 전까지 현재 시총) · 선정순서: 상위75 → 75~100위 기존 → "
                f"101~125위 기존(버퍼) → 남은 자리 비편입 · 100위 시총 ${(us['ndx']['cut100'] or 0)/1e9:,.1f}B · "
                f"Fast Entry 기준(구성 40위) ${(us['ndx']['fe_bar'] or 0)/1e9:,.1f}B · 해외 ADR은 시뮬 제외(확인필요) · "
                f"버퍼 자격(직전 재구성 100위 이내) 반영 · 백테스트 — 2026-06 분기: 10/10 전부 적중, 2025-12 연례: 편입 6/6·편출 5/6")
        cols = [("판정", "판정", "text", 30), ("순위", "rank", "int", 7), ("시뮬 순위", "sim_rank", "int", 8),
                ("예상 구성", "예상구성", "text", 8), ("티커", "symbols", "text", 12),
                ("종목명", "name", "text", 34), ("편입", "member", "text", 6), ("시총($B)", "mcap_bn", "num1", 10),
                ("100위 대비", "vs_100위", "pct", 10), ("1주 수익률", "ret_7d", "pct", 9), ("1개월", "ret_30d", "pct", 9), ("3개월", "ret_91d", "pct", 9),
                ("국가", "country", "text", 14), ("섹터", "sector", "text", 18), ("상장연도", "ipoyear", "int", 9)]
        r = R.section(ws, 4, "편입 예상", len(cols))
        r = R.table(ws, r, t[t["판정"].str.match(r"^(편입|Fast)")], cols)
        r = R.section(ws, r, "편출 예상", len(cols))
        r = R.table(ws, r, t[t["판정"].str.startswith("편출")], cols)
        r = R.section(ws, r, "적격 순위 1~140", len(cols))
        R.table(ws, r, t, cols)

    # ===== MSCI Japan =====
    if "msci_jp" in kr:
        m, t, bt = kr["msci_jp"]["meta"], kr["msci_jp"]["table"], kr["msci_jp"]["backtest"]
        ws = wb.create_sheet("MSCI Japan")
        R.title(ws, "MSCI Japan (Standard) — 11월 리뷰 예상 (GIMI 규칙, DM)",
                f"기준선 C ${m['C_bn']:.1f}B(범위 {m['C_lo_bn']:.1f}~{m['C_hi_bn']:.1f}) → 편입: 시총 ≥${m['add_bn']:.1f}B·유동 ≥${m['add_ff_bn']:.1f}B·"
                f"회전율 ≥20% / 유지: 시총 ≥${m['keep_bn']:.1f}B·유동 ≥${m['keep_ff_bn']:.1f}B · C 는 8월 리뷰 역산 × 시장(TOPIX 달러) 변화 · "
                f"판정 시총 {m['window'][0]}~{m['window'][1]} 평균 · 구성 {m['n_members']}종목(iShares EWJ)")
        r = R.section(ws, 4, "워크포워드 백테스트 (직전 리뷰 C 로 다음 리뷰 예측 — MSCI 공식 변경목록 대조)", 10)
        r = R.table(ws, r, bt, [("리뷰", "review", "text", 9), ("실제 편입", "실제 편입", "text", 40), ("실제 편출", "실제 편출", "text", 60),
                                ("워크포워드 C($B)", "워크포워드 C($B)", "num1", 12), ("편입 적중", "워크포워드 편입 적중", "text", 9),
                                ("편출 적중", "워크포워드 편출 적중", "text", 9), ("오탐(입/출)", "워크포워드 오탐(입/출)", "text", 10),
                                ("예측 편입", "예측 편입", "text", 40), ("예측 편출", "예측 편출", "text", 60),
                                ("사후 최적 C($B)", "사후 최적 C($B)", "num1", 12)], verdict_col=None)
        cols = [("판정", "판정", "text", 30), ("일본 순위", "rank_jp", "int", 8), ("코드", "code", "text", 7),
                ("종목명", "name", "text", 28), ("편입", "member", "text", 6), ("시총($B)", "full_bn", "num1", 9),
                ("유동비율", "fif", "pct", 8), ("유동시총($B)", "ff_bn", "num1", 10), ("편입선 대비", "vs_add", "x", 9),
                ("유지선 대비", "vs_keep", "x", 9), ("회전율(12M)", "atvr", "pct", 9),
                ("1주", "ret_7d", "pct", 8), ("1개월", "ret_30d", "pct", 8), ("3개월", "ret_91d", "pct", 8),
                ("C 하단 시", "판정(C하단)", "text", 16), ("C 상단 시", "판정(C상단)", "text", 16)]
        r = R.section(ws, r, "편입 예상", len(cols))
        r = R.table(ws, r, t[t["판정"].str.startswith("편입")], cols)
        r = R.section(ws, r, "편출 예상", len(cols))
        r = R.table(ws, r, t[t["판정"].str.startswith("편출")], cols)
        r = R.section(ws, r, "구성종목 + 비구성 상위 (판정 시총 순)", len(cols))
        R.table(ws, r, t, cols)

    # ===== S&P 500 =====
    if "sp" in us:
        a, d = us["sp"]["adds"], us["sp"]["drops"]
        ws = wb.create_sheet("S&P 500")
        P = screen_us.PARAMS
        R.title(ws, "S&P 500 — 편입 후보(나스닥 상장) · 편출 위험",
                f"공식 기준(2026-07 방법론): 시총 ≥${P['sp_mcap_min']/1e9:,.1f}B · 유동시총 ≥50%=${P['sp_mcap_min']/2e9:,.1f}B·IWF ≥0.10"
                f"(SEC 공시 float 환산 — 최대 1년 지연·태깅오류 가능, 미달은 탈락 아닌 '확인') · "
                f"GAAP 순이익 최근 분기+4분기 합 흑자 · FALR ≥0.75·월 25만주(미반영) · 미국 도미사일·10-K 제출 · 상장 12개월(스핀오프 면제) · "
                f"편출 후 1년 재심사 제외 — 최종은 위원회 재량(섹터 균형). 백테스트: 2026년 편입 MRVL·FLEX·ILMN 3/3 필터 통과, "
                f"편출 POOL·CPB·TTD 3/3 위험군(0.28~0.31x) 포착, 스핀오프(HONA)는 스크린 밖")
        aa = a.copy()
        for c in ("latest_q_ni", "ttm_ni"):
            aa[c] = aa[c] / 1e6
        cols = [("판정", "판정", "text", 24), ("티커", "symbol", "text", 9), ("종목명", "name", "text", 34),
                ("시총($B)", "mcap_bn", "num1", 10), ("하한 대비", "vs_하한", "x", 9), ("도미사일", "도미사일", "text", 18),
                ("시즈닝", "시즈닝", "text", 16), ("최근분기 순이익($M)", "latest_q_ni", "num1", 14),
                ("4분기합 순이익($M)", "ttm_ni", "num1", 14), ("분기말", "ni_end", "text", 11), ("흑자요건", "흑자요건", "text", 14),
                ("S&P 400", "S&P400", "text", 14), ("1주 수익률", "ret_7d", "pct", 9), ("1개월", "ret_30d", "pct", 9), ("3개월", "ret_91d", "pct", 9), ("유동시총 환산($B)", "float_bn", "num1", 13),
                ("공시 기준일", "float_asof", "text", 11), ("IWF 추정", "iwf_est", "num2", 9),
                ("유동시총 요건", "유동시총요건", "text", 26), ("메모", "메모", "text", 40)]
        r = R.section(ws, 4, "편입 후보", len(cols))
        r = R.table(ws, r, aa, cols)
        cols2 = [("판정", "판정", "text", 24), ("티커", "symbol", "text", 9), ("종목명", "name", "text", 34),
                 ("거래소", "exchange", "text", 9), ("시총($B)", "mcap_bn", "num1", 10), ("하한 대비", "vs_하한", "x", 9),
                 ("1주 수익률", "ret_7d", "pct", 9), ("1개월", "ret_30d", "pct", 9), ("3개월", "ret_91d", "pct", 9)]
        r = R.section(ws, r, "편출 위험 (구성종목 시총 하위 25)", len(cols))
        R.table(ws, r, d, cols2)

    # ===== 방법론 =====
    ws = wb.create_sheet("방법론")
    R.title(ws, "방법론 · 파라미터 · 한계", "파란 글씨 = 사람이 바꾸는 입력값 (data/overrides/*.csv)")
    r = R.section(ws, 4, "데이터 소스", 2)
    r = R.kv(ws, r, [
        ("KOSPI200 구성", "KODEX 200 PDF (삼성자산운용 API)"),
        ("MSCI Korea 구성", "iShares EWY 보유종목 CSV (MSCI Korea 25/50 — 편입 여부 동일)"),
        ("Nasdaq-100 구성", "api.nasdaq.com list-type/nasdaq100"),
        ("S&P 500 구성", "SPDR SPY 일별 보유종목"),
        ("한국 시총·주가", "네이버 시총순위 API + 일봉(siseJson). KRX Open API 승인 후 교체 예정"),
        ("한국 유동비율", "DART 최대주주등 지분율 + 자기주식 (기타 전략적 보유 미반영 → 과대추정 가능)"),
        ("FTSE Korea 구성", "Vanguard VEU 보유종목 JSON (FTSE All-World ex US, 월말·지연)"),
        ("한국 산업군", "네이버 업종(WICS 계열) → KRICS 9개/GICS 10개 근사 + krics_fix.csv 보정, KRX 공식 KRICS 공개(10/26) 후 krics.csv 최우선"),
        ("MSCI 공식 변경", "app2.msci.com/eqb/gimi/stdindex/MSCI_{Mon}{YY}_STPublicList.pdf"),
        ("미국 시총", "nasdaq.com screener (회사 단위 시총)"),
        ("미국 GAAP 순이익", "SEC XBRL companyconcept (NetIncomeLoss 등, 10-Q/10-K)"),
    ])
    r = R.section(ws, r, "KOSPI200·MSCI 파라미터 (data/overrides/kr_params.csv)", 2)
    r = R.kv(ws, r, [(k, "" if v is None else v) for k, v in screen_kr._params().items()],
             blue_keys=set(screen_kr.PARAMS))
    r = R.section(ws, r, "Nasdaq-100·S&P 500 파라미터 (screen_us.PARAMS)", 2)
    r = R.kv(ws, r, [(k, str(v)) for k, v in screen_us.PARAMS.items()], blue_keys=set(screen_us.PARAMS))
    r = R.section(ws, r, "한계", 2)
    R.kv(ws, r, [
        ("일평균 시총", "과거 종가 × 현재 상장주식수 근사 — 기간 중 증자·소각·분할 종목은 오차"),
        ("KOSPI200 규칙", "6개월 일평균시총 → 산업군 누적85%(1차) → 기존 110%·신규 90% 이내(2차, 반올림) → 200개 맞춤(3차) → 15일 평균 50위 특례. "
                        "유동성: 산업군 내 거래대금 85% 이내. 제외: 관리·리츠·스팩·유동비율<10%·상장 6개월 미만(분할 신설 구성종목 예외)"),
        ("KOSPI200 한계", "산업군 분류가 근사라 경계 종목은 분류에 따라 뒤집힘 → KRICS·구GICS 둘 다 같은 판정이면 '유력', 하나만이면 '경합/위험'"),
        ("MSCI 규칙", "편입: 시총 ≥1.5C·유동 ≥0.5C·외국인여유 ≥15%·회전율 ≥15% / 유지: 시총 ≥2/3C·유동 ≥1/3C·여유 ≥3.75%·회전율 ≥10%"),
        ("MSCI 한계", "C(기준선)는 MSCI 비공개 → 증권사 추정 역산값. 유동비율은 DART 최대주주 기준이라 전략적 보유가 큰 종목은 과대. "
                     "우선주 라인(삼성전자우 등)은 회사 단위로 따라가므로 별도 판정하지 않음"),
        ("FTSE 한계", "VEU/VSS 스냅샷은 직전 월말(지연)이라 직후 리뷰 변경이 빠질 수 있음. 지역 86%/92% 백분위를 이론대로 계산하면 "
                     "호주·대만 고유동비율로 기준선이 부풀어(실측) 한국 앵커(승격 안 된 소형 상위/현 구성 하위 분포)로 대체 — 참고용. "
                     "비구성 후보는 외국인 한도 여유 20% 미만이면 편입 불가(KT 등)"),
        ("S&P 500", "위원회 재량(섹터 균형 등) — 요건 충족 후보 리스트이지 확정 예측이 아님"),
        ("Nasdaq-100", "101~125위 버퍼는 '직전 재구성 때 100위 이내였거나 그 뒤 편입된 종목'만 해당 — 작년 100위 밖에서 버퍼로 남은 종목은 "
                       "올해 다시 100위 안에 못 들면 편출. 현재는 전 구성종목이 버퍼 자격이 있다고 가정. 해외 ADR(본국 주상장)은 "
                       "미국 상장 예탁주식분만으로 순위가 매겨져 screener 시총보다 훨씬 작음 → 확인필요로 분리"),
        ("S&P 500 요건", "유동시총·IWF 는 SEC 10-K 표지 공시(dei:EntityPublicFloat)를 가격비율로 현재가 환산 — 기준일이 직전 회계연도 "
                       "2분기 말이라 최대 1년 지연, 일부 태깅오류(ALAB 등) → 미달은 '확인' 표시(탈락 아님). FALR 유동성 비율은 미반영. "
                       "미국 도미사일은 nasdaq.com 국가 필드 기준(케이맨 등 설립지 예외 있음)"),
    ])
    for w in ("A", "B"):
        ws.column_dimensions[w].width = 28 if w == "A" else 90
    R.finish(wb, path)
    log(f"[report] {path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true")
    a = ap.parse_args()
    main(fetch=not a.no_fetch)
