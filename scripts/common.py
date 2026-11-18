"""지수편입 트래커 공용 모듈: 경로·키·HTTP·숫자 파싱."""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SNAP = DATA / "snapshots"
CACHE = DATA / "cache"
OVR = DATA / "overrides"
OUT = ROOT / "output"
LOGS = ROOT / "logs"
for p in (SNAP, CACHE, OVR, OUT, LOGS):
    p.mkdir(parents=True, exist_ok=True)

ENV_PATH = Path(os.environ.get("INDEX_TRACKER_ENV", str(ROOT / ".env")))  # 선택: KRX_API_KEY 등. 없어도 실행됨

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
SEC_UA = os.environ.get("SEC_USER_AGENT", "index-tracker research-bot@example.com")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def env(name: str) -> str | None:
    """공용 .env 에서 키를 읽는다. 값은 절대 출력하지 않는다."""
    try:
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    except FileNotFoundError:
        return None
    return None


_S = requests.Session()
_S.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9,ko;q=0.8"})


def get(url: str, *, params=None, headers=None, timeout=40, tries=3, pause=1.5):
    last = None
    for i in range(tries):
        try:
            r = _S.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code == 200:
                return r
            last = RuntimeError(f"HTTP {r.status_code} {url}")
        except requests.RequestException as e:
            last = e
        time.sleep(pause * (i + 1))
    raise last


def num(x) -> float | None:
    """'1,234', '$5.6', '12.3%', 'N/A' → float/None."""
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip().replace(",", "").replace("$", "").replace("%", "")
    if s in ("", "-", "N/A", "NA", "None", "nan"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def today() -> dt.date:
    return dt.date.today()


def ymd(d: dt.date) -> str:
    return d.strftime("%Y%m%d")


def snap_dir(d: dt.date | None = None) -> Path:
    p = SNAP / ymd(d or today())
    p.mkdir(parents=True, exist_ok=True)
    return p


def latest_snap_before(name: str, d: dt.date) -> Path | None:
    """d 이전 날짜 스냅샷 중 name 파일이 있는 가장 최근 것."""
    cands = sorted(p for p in SNAP.iterdir() if p.is_dir() and p.name < ymd(d) and (p / name).exists())
    return cands[-1] / name if cands else None


def read_csv(path: Path, **kw):
    """티커 'NA'·종목코드 앞자리 0 이 깨지지 않게 읽는다 (빈칸만 결측)."""
    import pandas as pd
    kw.setdefault("dtype", {"symbol": str, "code": str})
    return pd.read_csv(path, keep_default_na=False, na_values=[""], **kw)


def save_json(obj, path: Path):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


def load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def log(msg: str):
    line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(LOGS / f"run_{ymd(today())}.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")
