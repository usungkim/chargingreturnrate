"""
charging_return_rate_v2.py
==========================
EV Infra 충전 복귀율 계산 툴 v2 (pandas + PostgreSQL, Streamlit 호환).

v1 → v2 변경 사항
------------------
[유효 충전 정의 변경]
  v1: pay_result = true AND charging_fee > 0
  v2: end_datetime > start_datetime AND charging_kw > 0.01 AND charging_fee > 10

[버그 수정 목록]
  BUG-01  band_hi 경계값 오류
          v1: last_charge < band_hi  → T-low 당일 충전자가 코호트에서 빠짐
          v2: last_charge <= band_hi (포함) 로 수정

  BUG-02  from __future__ import annotations 위치 오류
          v1: load_dotenv() 이후에 선언 → SyntaxError
          v2: 파일 최상단으로 이동

  BUG-03  silence_days와 SQL band 경계 불일치
          v1: pd.cut bins=range(low, high+1, step) → SQL의 < band_hi 와 off-by-one
          v2: bins 경계를 [T-high, T-high+step, ..., T-low] 로 SQL과 동일하게 맞춤
              (silence_days 기준: low 이상 high 이하 포함)

  BUG-04  소버킷 n 부족 시 z-test 결과 무경고 출력
          v2: np < 5 또는 n(1-p) < 5 인 버킷에 warning 플래그 추가

  BUG-05  summary() 에서 None 값 포맷팅 오류
          v1: flag_rate / control_rate / gap_pp 가 None 일 때 f-string 에서 TypeError
          v2: None 에 대한 안전한 포맷 처리

  BUG-06  return_window_days 하드코딩 불일치
          v1: 파라미터로 받으면서 docstring에 "60일"이라고만 적혀 있어 혼선
          v2: RETURN_WINDOW_DAYS = 60 상수로 고정 명시 (정의서 기준)

고정 가정 (변경 금지)
  - 유효 충전 시점  : start_datetime 기준 (KST naive, 변환 금지)
  - 복귀 판정 시점  : start_datetime >= T AND start_datetime < T+60일
  - 침묵 일수       : T - last_charge_date (일 단위, 정수)
  - 회원 필터       : active_only=True 이면 mb_status='active'

필요 패키지: pandas, sqlalchemy, psycopg2-binary, python-dotenv
"""
from __future__ import annotations  # BUG-02 수정: 반드시 파일 최상단

import math
import warnings
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd
from sqlalchemy import text

# ── 고정 상수 ────────────────────────────────────────────────────────
# BUG-06: 정의서 기준 60일 고정. 바꾸려면 여기만 수정.
RETURN_WINDOW_DAYS: int = 60

# v2 유효 충전 조건 (변경 금지)
# charging_kw, charging_fee 가 DB에서 varchar로 저장된 경우를 대비해 NUMERIC 캐스트
VALID_CHARGE_CLAUSE = """
    end_datetime > start_datetime
    AND charging_kw::numeric > 0.01
    AND charging_fee::numeric > 10
""".strip()


# ── 유틸 ─────────────────────────────────────────────────────────────
def load_flag_ids(path: str) -> set[int]:
    """플래그군 CSV(한 줄에 mb_id 하나, 헤더 있어도 무방)에서 정수 ID만 추출."""
    ids: set[int] = set()
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            tok = line.strip().split(",")[0].strip()
            if tok.lstrip("-").isdigit():
                ids.add(int(tok))
    return ids


def _two_prop_z(s1: int, n1: int, s2: int, n2: int) -> tuple[float, float]:
    """두 비율 차이의 pooled z-검정. (z, 양측 p) 반환."""
    if n1 == 0 or n2 == 0:
        return 0.0, 1.0
    p1, p2 = s1 / n1, s2 / n2
    p_pool = (s1 + s2) / (n1 + n2)
    se = math.sqrt(p_pool * (1 - p_pool) * (1 / n1 + 1 / n2))
    if se == 0:
        return 0.0, 1.0
    z = (p1 - p2) / se
    pval = math.erfc(abs(z) / math.sqrt(2))  # 양측, scipy 없이
    return z, pval


def _normality_ok(n: int, p: float) -> bool:
    """z-test 정규근사 조건: np >= 5 AND n(1-p) >= 5"""
    if n == 0:
        return False
    return (n * p >= 5) and (n * (1 - p) >= 5)


def _fmt(val, fmt=".1f", fallback="N/A") -> str:
    """BUG-05 수정: None-safe 포맷터."""
    if val is None:
        return fallback
    return format(val, fmt)


# ── 결과 컨테이너 ─────────────────────────────────────────────────────
@dataclass
class ReturnRateResult:
    params: dict
    flag_total: int           # CSV의 플래그 ID 총수
    flag_in_cohort: int       # 그중 코호트 안에 있던 수
    flag_dropped: int         # 코호트 밖이라 제외된 수
    baseline: dict            # 코호트 전체 복귀율
    pooled: dict              # 플래그 vs 대조 (전체 합산 + z검정)
    by_bucket: pd.DataFrame   # 소버킷 × 그룹 표 (곡선용)
    warnings: list[str] = field(default_factory=list)  # BUG-04: 경고 목록

    def summary(self) -> str:
        p = self.params
        lo, hi = p["last_charge_band"]

        # BUG-05 수정: None-safe 포맷
        flag_rate_str    = _fmt(self.pooled.get("flag_rate"))
        control_rate_str = _fmt(self.pooled.get("control_rate"))
        gap_str          = _fmt(self.pooled.get("gap_pp"), fmt="+.1f")

        lines = [
            "═" * 60,
            f"  T = {p['T']}  |  침묵밴드 {lo}–{hi}일  |  복귀창 {RETURN_WINDOW_DAYS}일",
            f"  active_only={p['active_only']}"
            + (f"  |  행동윈도우(메타) {p['behavior_window_days']}일"
               if p.get("behavior_window_days") else ""),
            "═" * 60,
            f"  플래그 CSV: {self.flag_total}명 중 코호트 내 {self.flag_in_cohort}명"
            f" (제외 {self.flag_dropped}명)",
            f"  코호트 baseline: n={self.baseline['n']:,}  "
            f"복귀 {self.baseline['returners']:,}  →  {_fmt(self.baseline['rate'])}%",
            "─" * 60,
            f"  [전체 합산]",
            f"    플래그  {flag_rate_str}%  (n={self.pooled.get('flag_n', 0):,})",
            f"    대조군  {control_rate_str}%  (n={self.pooled.get('control_n', 0):,})",
            f"    gap = {gap_str}%p   z = {self.pooled['z']}   p = {self.pooled['p']}"
            + ("   ★유의(p<0.05)" if self.pooled["p"] < 0.05 else "   (비유의)"),
        ]

        # BUG-04: 경고 출력
        if self.warnings:
            lines.append("─" * 60)
            lines.append("  ⚠️  통계 경고 (z-test 근사 불안정 버킷)")
            for w in self.warnings:
                lines.append(f"    {w}")

        lines += [
            "─" * 60,
            "  [소버킷 곡선]",
            self.by_bucket.to_string(index=False),
            "═" * 60,
        ]
        return "\n".join(lines)


# ── 메인 함수 ─────────────────────────────────────────────────────────
def compute_return_rate(
    engine,
    flag_csv_path: str,
    T: str | date | None = None,
    last_charge_band: tuple[int, int] = (90, 180),
    sub_bucket_days: int | None = 30,
    active_only: bool = True,
    behavior_window_days: Optional[int] = None,
    today: Optional[date] = None,
) -> ReturnRateResult:
    """충전 복귀율을 계산한다.

    Parameters
    ----------
    engine            : SQLAlchemy engine (PostgreSQL)
    flag_csv_path     : 플래그군 mb_id CSV 경로
    T                 : 기준 시점 (yyyy-mm-dd). None이면 오늘 - RETURN_WINDOW_DAYS.
    last_charge_band  : (low, high) 침묵 밴드 (일). 기본 (90, 180).
    sub_bucket_days   : 소버킷 단위 (일). None이면 단일 버킷.
    active_only       : True이면 mb_status='active' 필터
    behavior_window_days : 메타 라벨용 (CSV 생성 윈도우 기록)
    today             : 테스트용 날짜 주입. None이면 date.today().

    Notes
    -----
    - 복귀창은 RETURN_WINDOW_DAYS(=60)일 고정.
    - T_end = T + 60일이 오늘 이후이면 ValueError.
    - 침묵 밴드 경계: last_charge >= T-high AND last_charge <= T-low  ← BUG-01 수정
    """
    today = today or date.today()
    low, high = last_charge_band

    # ── T 결정 + 관측가능성 가드 ──────────────────────────────────────
    if T is None:
        T = today - timedelta(days=RETURN_WINDOW_DAYS)
    elif isinstance(T, str):
        T = datetime.strptime(T, "%Y-%m-%d").date()

    T_end = T + timedelta(days=RETURN_WINDOW_DAYS)
    if T_end > today:
        raise ValueError(
            f"복귀창이 아직 닫히지 않았습니다: "
            f"T={T}, T+{RETURN_WINDOW_DAYS}일={T_end} > 오늘({today}). "
            f"T를 {today - timedelta(days=RETURN_WINDOW_DAYS)} 이하로 설정하세요."
        )

    # ── 날짜 경계 계산 ────────────────────────────────────────────────
    # BUG-01 수정: band_hi를 T-low 당일 23:59:59.999...까지 포함
    #   침묵 low일 = T - low일 (당일 포함)
    #   침묵 high일 = T - high일 (당일 포함)
    band_lo_dt  = datetime.combine(T - timedelta(days=high), datetime.min.time())
    # T-low 다음날 00:00:00 미만 = T-low 당일 전체 포함
    band_hi_dt  = datetime.combine(T - timedelta(days=low) + timedelta(days=1), datetime.min.time())
    T_dt        = datetime.combine(T, datetime.min.time())
    T_end_dt    = datetime.combine(T_end, datetime.min.time())

    # ── SQL ──────────────────────────────────────────────────────────
    active_clause = "AND m.mb_status = 'active'" if active_only else ""

    sql = text(f"""
        WITH lastc AS (
            -- 기준 시점 T 이전의 마지막 유효 충전
            SELECT
                mb_id,
                MAX(start_datetime) AS last_charge
            FROM charging_history
            WHERE
                {VALID_CHARGE_CLAUSE}
                AND start_datetime < :T
            GROUP BY mb_id
        ),
        coh AS (
            -- 침묵 밴드 [T-high, T-low] 에 해당하는 코호트
            -- BUG-01 수정: band_hi_dt = T-low+1일 로 T-low 당일 포함
            SELECT
                l.mb_id,
                l.last_charge
            FROM lastc l
            JOIN member m ON m.mb_id = l.mb_id
            WHERE
                l.last_charge >= :band_lo
                AND l.last_charge < :band_hi
                {active_clause}
        ),
        ret AS (
            -- 복귀창 [T, T+60일) 내 유효 충전 여부
            SELECT DISTINCT mb_id
            FROM charging_history
            WHERE
                {VALID_CHARGE_CLAUSE}
                AND start_datetime >= :T
                AND start_datetime < :T_end
        )
        SELECT
            c.mb_id,
            c.last_charge::date AS last_charge_date,
            CASE WHEN r.mb_id IS NOT NULL THEN 1 ELSE 0 END AS returned
        FROM coh c
        LEFT JOIN ret r ON r.mb_id = c.mb_id
    """)

    df = pd.read_sql(sql, engine, params={
        "T":       T_dt,
        "T_end":   T_end_dt,
        "band_lo": band_lo_dt,
        "band_hi": band_hi_dt,
    })

    if df.empty:
        raise ValueError(
            f"코호트가 비어 있습니다. T={T}, 침묵밴드={low}-{high}일을 확인하세요."
        )

    # ── mb_id 타입 정규화 ─────────────────────────────────────────────
    df["mb_id"] = pd.to_numeric(df["mb_id"], errors="coerce").astype("Int64")
    df = df.dropna(subset=["mb_id"]).astype({"mb_id": "int64"})
    cohort_ids = set(df["mb_id"])

    # ── 플래그 매칭 ───────────────────────────────────────────────────
    flag_all = load_flag_ids(flag_csv_path)
    flag_in  = flag_all & cohort_ids
    df["grp"] = df["mb_id"].isin(flag_in).map({True: "flag", False: "control"})

    # ── 침묵 일수 + 소버킷 ────────────────────────────────────────────
    # BUG-03 수정: silence_days 계산을 SQL band 경계와 일치
    #   SQL: last_charge >= T-high  AND  last_charge < T-low+1
    #   → silence_days 범위: [low, high] (양 끝 포함)
    df["silence_days"] = (
        pd.Timestamp(T) - pd.to_datetime(df["last_charge_date"])
    ).dt.days

    if sub_bucket_days:
        # edges: low, low+step, ..., high (양 끝 포함)
        edges = list(range(low, high, sub_bucket_days))
        if not edges or edges[-1] < high:
            edges.append(high)
        # SQL과 동일하게: low 이상 high 이하
        labels = [f"{edges[i]}–{edges[i+1]}일" for i in range(len(edges) - 1)]
        df["bucket"] = pd.cut(
            df["silence_days"],
            bins=edges,
            labels=labels,
            include_lowest=True,  # 첫 bin 좌측 포함 (low 포함)
            right=True,           # 우측 닫힘 (high 포함)
        )
    else:
        df["bucket"] = f"{low}–{high}일"

    # ── 소버킷 × 그룹 집계 ───────────────────────────────────────────
    by_bucket = (
        df.groupby(["bucket", "grp"], observed=True)["returned"]
        .agg(n="size", returners="sum")
        .reset_index()
    )
    by_bucket["rate"] = (100 * by_bucket["returners"] / by_bucket["n"]).round(1)

    # BUG-04: 정규근사 조건 미충족 버킷 경고
    stat_warnings: list[str] = []
    for _, row in by_bucket.iterrows():
        p_hat = row["returners"] / row["n"] if row["n"] > 0 else 0
        if not _normality_ok(int(row["n"]), p_hat):
            stat_warnings.append(
                f"bucket={row['bucket']} / grp={row['grp']} → "
                f"n={row['n']}, p={p_hat:.3f} "
                f"(np={row['n']*p_hat:.1f}, n(1-p)={row['n']*(1-p_hat):.1f}) "
                f"— z검정 근사 불안정"
            )
    if stat_warnings:
        warnings.warn(
            "일부 소버킷에서 z-test 정규근사 조건 미충족 (np < 5 또는 n(1-p) < 5). "
            "결과 해석 시 주의하세요.",
            stacklevel=2,
        )

    # ── 전체 합산 플래그 vs 대조 z-검정 ──────────────────────────────
    f_ser = df.loc[df.grp == "flag",    "returned"]
    c_ser = df.loc[df.grp == "control", "returned"]
    z, pval = _two_prop_z(int(f_ser.sum()), len(f_ser), int(c_ser.sum()), len(c_ser))

    pooled = {
        "flag_n":        len(f_ser),
        "flag_rate":     round(100 * f_ser.mean(), 1) if len(f_ser) else None,
        "control_n":     len(c_ser),
        "control_rate":  round(100 * c_ser.mean(), 1) if len(c_ser) else None,
        "gap_pp":        round(100 * (f_ser.mean() - c_ser.mean()), 1)
                         if len(f_ser) and len(c_ser) else None,
        "z":             round(z, 2),
        "p":             round(pval, 4),
        "z_test_valid":  _normality_ok(len(f_ser), f_ser.mean() if len(f_ser) else 0)
                         and _normality_ok(len(c_ser), c_ser.mean() if len(c_ser) else 0),
    }

    baseline = {
        "n":         len(df),
        "returners": int(df["returned"].sum()),
        "rate":      round(100 * df["returned"].mean(), 1) if len(df) else None,
    }

    return ReturnRateResult(
        params={
            "T":                   str(T),
            "T_end":               str(T_end),
            "last_charge_band":    last_charge_band,
            "sub_bucket_days":     sub_bucket_days,
            "active_only":         active_only,
            "behavior_window_days": behavior_window_days,
        },
        flag_total=len(flag_all),
        flag_in_cohort=len(flag_in),
        flag_dropped=len(flag_all) - len(flag_in),
        baseline=baseline,
        pooled=pooled,
        by_bucket=by_bucket,
        warnings=stat_warnings,
    )


# ── DB 엔진 팩토리 (공통 사용) ───────────────────────────────────────
def get_engine():
    """
    .env 파일(또는 환경변수)에서 PG_DSN을 읽어 SQLAlchemy engine을 반환한다.
    Streamlit / 로컬 CLI 양쪽에서 동일하게 호출한다.

    .env 예시:
        PG_DSN=postgresql+psycopg2://user:password@host:5432/dbname

    Streamlit Cloud 사용 시:
        .streamlit/secrets.toml 에 PG_DSN = "..." 형태로 저장.
        (secrets.toml 은 .gitignore 로 추적 제외)
    """
    import os
    from dotenv import load_dotenv
    from sqlalchemy import create_engine

    load_dotenv()  # .env 파일 우선, 없으면 시스템 환경변수 사용
    dsn = os.environ.get("PG_DSN")
    if not dsn:
        raise EnvironmentError(
            "환경변수 PG_DSN 이 설정되지 않았습니다.\n"
            ".env 파일을 확인하거나 export PG_DSN='...' 으로 설정하세요.\n"
            "템플릿: .env.example 참조"
        )
    return create_engine(dsn)


# ── 로컬 실행 예시 ────────────────────────────────────────────────────
if __name__ == "__main__":
    engine = get_engine()

    res = compute_return_rate(
        engine,
        flag_csv_path="GroupB_0609.csv",
        T="2026-04-01",
        last_charge_band=(90, 180),
        sub_bucket_days=30,
        behavior_window_days=14,
    )
    print(res.summary())