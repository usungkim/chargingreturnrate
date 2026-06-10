"""
app.py
======
EV Infra 충전 복귀율 계산기 — Streamlit UI

실행:
    streamlit run app.py

의존:
    charging_return_rate_v2.py, .env (PG_DSN)
"""

import io
import tempfile
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from charging_return_rate_v2 import (
    RETURN_WINDOW_DAYS,
    compute_return_rate,
    get_engine,
)

# ── 페이지 설정 ───────────────────────────────────────────────────────
st.set_page_config(
    page_title="충전 복귀율 계산기",
    page_icon="⚡",
    layout="wide",
)

st.title("⚡ 충전 복귀율 계산기")
st.caption(
    f"코호트의 침묵 밴드별 복귀율을 계산합니다. "
    f"복귀창은 **{RETURN_WINDOW_DAYS}일** 고정입니다."
)

# ── 사이드바: 파라미터 입력 ───────────────────────────────────────────
with st.sidebar:
    st.header("📋 파라미터 설정")

    # 기준 시점 T
    default_T = date.today() - timedelta(days=RETURN_WINDOW_DAYS)
    T = st.date_input(
        "기준 시점 T",
        value=default_T,
        max_value=default_T,   # T_end > 오늘이면 ValueError
        help=f"복귀창({RETURN_WINDOW_DAYS}일)이 오늘 이전에 닫혀야 합니다. 최대: {default_T}",
    )

    st.divider()

    # 침묵 밴드
    st.subheader("침묵 밴드 (일)")
    col_lo, col_hi = st.columns(2)
    with col_lo:
        band_low = st.number_input("최솟값", min_value=1, max_value=999, value=90, step=1)
    with col_hi:
        band_high = st.number_input("최댓값", min_value=1, max_value=999, value=180, step=1)

    if band_low >= band_high:
        st.error("최솟값이 최댓값보다 작아야 합니다.")

    st.divider()

    # 소버킷
    use_sub = st.checkbox("소버킷 분할", value=True)
    sub_bucket_days = None
    if use_sub:
        sub_bucket_days = st.number_input(
            "소버킷 단위 (일)",
            min_value=1,
            max_value=band_high - band_low,
            value=min(30, band_high - band_low),
            step=1,
        )

    st.divider()

    # 회원 필터
    active_only = st.checkbox("활성 회원만 (mb_status = active)", value=True)

    st.divider()

    # 행동 윈도우 (메타)
    use_bw = st.checkbox("행동 윈도우 기록 (메타)", value=False)
    behavior_window_days = None
    if use_bw:
        behavior_window_days = st.number_input(
            "행동 윈도우 (일)",
            min_value=1, max_value=365, value=14, step=1,
        )

    st.divider()

    # 플래그군 CSV
    st.subheader("플래그군 CSV")
    uploaded_csv = st.file_uploader(
        "mb_id 목록 CSV",
        type=["csv"],
        help="한 컬럼에 mb_id(정수)만 있으면 됩니다. 헤더 있어도 무방.",
    )

    st.divider()

    # 실행 버튼
    run_btn = st.button("🚀 계산 실행", type="primary", use_container_width=True)


# ── 메인 영역 ─────────────────────────────────────────────────────────
if not run_btn:
    st.info("왼쪽 사이드바에서 파라미터를 설정하고 **계산 실행** 버튼을 누르세요.")
    st.stop()

# 입력 검증
if band_low >= band_high:
    st.error("침묵 밴드: 최솟값 < 최댓값 조건을 확인하세요.")
    st.stop()

if uploaded_csv is None:
    st.error("플래그군 CSV 파일을 업로드하세요.")
    st.stop()

# ── 계산 실행 ─────────────────────────────────────────────────────────
with st.spinner("DB 조회 중..."):
    try:
        engine = get_engine()

        # CSV를 임시 파일로 저장 (compute_return_rate가 경로를 받으므로)
        with tempfile.NamedTemporaryFile(
            delete=False, suffix=".csv", mode="wb"
        ) as tmp:
            tmp.write(uploaded_csv.read())
            tmp_path = tmp.name

        res = compute_return_rate(
            engine,
            flag_csv_path=tmp_path,
            T=str(T),
            last_charge_band=(int(band_low), int(band_high)),
            sub_bucket_days=int(sub_bucket_days) if sub_bucket_days else None,
            active_only=active_only,
            behavior_window_days=int(behavior_window_days) if behavior_window_days else None,
        )

    except ValueError as e:
        st.error(f"파라미터 오류: {e}")
        st.stop()
    except EnvironmentError as e:
        st.error(f"DB 접속 오류: {e}")
        st.stop()
    except Exception as e:
        st.error(f"계산 중 오류 발생: {e}")
        st.stop()

# ── 통계 경고 ─────────────────────────────────────────────────────────
if res.warnings:
    with st.expander("⚠️ 통계 경고 (z-test 근사 불안정 버킷)", expanded=True):
        for w in res.warnings:
            st.warning(w)

# ── 요약 지표 카드 ────────────────────────────────────────────────────
st.subheader("📊 요약")

m1, m2, m3, m4 = st.columns(4)
m1.metric(
    "코호트 전체",
    f"{res.baseline['n']:,}명",
    help="침묵 밴드 내 전체 사용자 수",
)
m2.metric(
    "baseline 복귀율",
    f"{res.baseline['rate']}%",
    help=f"코호트 전체에서 {RETURN_WINDOW_DAYS}일 내 복귀한 비율",
)
m3.metric(
    "플래그군 복귀율",
    f"{res.pooled.get('flag_rate', 'N/A')}%",
    delta=f"{res.pooled.get('gap_pp', 0):+.1f}%p vs 대조" if res.pooled.get("gap_pp") is not None else None,
    help=f"CSV 내 코호트 매칭: {res.flag_in_cohort:,}명 / {res.flag_total:,}명",
)
m4.metric(
    "대조군 복귀율",
    f"{res.pooled.get('control_rate', 'N/A')}%",
    help=f"대조군 n={res.pooled.get('control_n', 0):,}명",
)

# ── z-test 결과 ───────────────────────────────────────────────────────
p_val = res.pooled["p"]
z_val = res.pooled["z"]
is_sig = p_val < 0.05
z_valid = res.pooled.get("z_test_valid", True)

sig_label = "★ 유의 (p < 0.05)" if is_sig else "비유의 (p ≥ 0.05)"
sig_color = "green" if is_sig else "gray"

st.markdown(
    f"**z-test 결과:** z = `{z_val}` &nbsp; p = `{p_val}` &nbsp; "
    f"→ :{sig_color}[{sig_label}]"
    + (" &nbsp; ⚠️ n 부족으로 근사 불안정" if not z_valid else ""),
    unsafe_allow_html=False,
)

st.divider()

# ── 소버킷 표 ─────────────────────────────────────────────────────────
st.subheader("📋 버킷별 복귀율 표")

display_df = res.by_bucket.copy()
display_df.columns = ["침묵 버킷", "그룹", "인원 수", "복귀자 수", "복귀율 (%)"]
display_df["인원 수"]   = display_df["인원 수"].apply(lambda x: f"{x:,}")
display_df["복귀자 수"] = display_df["복귀자 수"].apply(lambda x: f"{x:,}")

st.dataframe(
    display_df,
    use_container_width=True,
    hide_index=True,
)

st.divider()

# ── 소버킷 막대그래프 ─────────────────────────────────────────────────
st.subheader("📈 버킷별 복귀율 그래프")

import plotly.graph_objects as go

chart_df = res.by_bucket.copy()
chart_df["bucket"] = chart_df["bucket"].astype(str)

# X축 정렬: 버킷 라벨 앞 숫자(low) 기준 오름차순
chart_df["_sort_key"] = chart_df["bucket"].str.extract(r"^(\d+)").astype(int)
chart_df = chart_df.sort_values("_sort_key")
bucket_order = chart_df["bucket"].unique().tolist()

# 플래그 / 대조 분리
flag_df    = chart_df[chart_df["grp"] == "flag"]
control_df = chart_df[chart_df["grp"] == "control"]

fig = go.Figure()
fig.add_trace(go.Scatter(
    x=flag_df["bucket"], y=flag_df["rate"],
    mode="lines+markers", name="플래그군",
    line=dict(color="#E87B4C", width=2),
    marker=dict(size=7),
))
fig.add_trace(go.Scatter(
    x=control_df["bucket"], y=control_df["rate"],
    mode="lines+markers", name="대조군",
    line=dict(color="#4C9BE8", width=2),
    marker=dict(size=7),
))
fig.update_layout(
    xaxis=dict(
        categoryorder="array",
        categoryarray=bucket_order,   # 정렬된 순서 고정
        tickangle=0,                  # X축 라벨 가로
        title="침묵 버킷 (일)",
    ),
    yaxis=dict(title="복귀율 (%)"),
    legend=dict(orientation="h", y=-0.2),
    margin=dict(t=20, b=60),
    height=380,
)
st.plotly_chart(fig, use_container_width=True)

st.divider()

# ── CSV 다운로드 ──────────────────────────────────────────────────────
st.subheader("⬇️ 결과 다운로드")

csv_buf = io.StringIO()
res.by_bucket.to_csv(csv_buf, index=False, encoding="utf-8-sig")

st.download_button(
    label="버킷별 결과 CSV 다운로드",
    data=csv_buf.getvalue().encode("utf-8-sig"),
    file_name=f"return_rate_T{T}_band{band_low}-{band_high}.csv",
    mime="text/csv",
)