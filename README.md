# ⚡ Charging Return Rate

EV 충전 사용자의 **침묵 밴드별 복귀율**을 계산하는 분석 툴입니다.  
PostgreSQL에 연결해 코호트를 구성하고, 플래그군(실험군)과 대조군의 복귀율 차이를 two-proportion z-test로 검증합니다.  
CLI 스크립트(`charging_return_rate_v2.py`)와 Streamlit 대시보드(`app.py`) 두 가지 인터페이스를 제공합니다.

---

## 목적 

### 분석 배경 (Why this tool exists)

이 툴은 EV 충전 인프라 서비스(EV Infra)의 이탈 사용자 재활성화 전략을 수립하는 과정에서 만들어졌습니다.

#### 문제 인식

MAU 정체 원인을 분석하던 중, 신규 유입보다 이탈 복귀율이 더 큰 레버임을 확인했습니다. 문제는 어떤 행동을 한 사용자가 실제로 복귀하는지 인과적으로 검증할 방법이 없었다는 점입니다.

#### 분석 설계

이탈 정의부터 재정의했습니다. 단순 미접속이 아닌 "충전 침묵 밴드(90–180일)" — 마지막 유효 충전 이후 90일 이상 180일 이하로 충전이 없었던 사용자 — 를 코호트로 정의했습니다.
| 90일 미만: 일시적 이탈로, 자연 복귀율이 높아 액션 효과를 측정하기 어려움
| 180일 초과: 사실상 영구 이탈에 가까워 복귀 가능성이 낮음
| 침묵 밴드 내 복귀율 곡선을 먼저 그려보니 90일 시점에서 약 52%의 복귀 확률이 관찰되었고, 일수가 길어질수록 단조 감소하는 패턴을 확인했습니다.

#### 핵심 발견

앱 내 검색(search) 행동을 침묵 기간 중 1회 이상 수행한 사용자 그룹(n=818)과 대조군의 복귀율을 비교한 결과:
지표플래그군 (검색 O)대조군 (검색 X)복귀율47.6%40.2%차이+7.4%p—z-testz = 2.41, p = 0.016유의 (p < 0.05) 검색 행동이 **재활성화의 선행 지표(leading indicator)**임을 통계적으로 검증했습니다. 반면 혜택 탭 조회 등 다른 행동은 표본이 부족하거나 유의하지 않아 기각했습니다.

#### 활용 

이 분석을 일회성 쿼리로 끝내지 않고, 다른 행동 변수나 다른 침묵 밴드 구간에도 동일한 방법론을 반복 적용할 수 있도록 재사용 가능한 툴로 패키징했습니다. 플래그군 CSV만 교체하면 어떤 행동 세그먼트든 동일한 통계 프레임으로 검증할 수 있습니다.

## 분석 개요

| 개념 | 정의 |
|---|---|
| **기준 시점 T** | 복귀 여부를 판단하는 시작 날짜 |
| **침묵 밴드** | T 기준으로 마지막 유효 충전이 `[T-high, T-low]` 범위에 속하는 사용자 집합 (기본: 90–180일) |
| **복귀창** | T 이후 60일 고정 (`RETURN_WINDOW_DAYS = 60`) |
| **복귀 정의** | 복귀창 내 유효 충전 1회 이상 (`start_datetime >= T AND < T+60일`) |
| **유효 충전 조건** | `end_datetime > start_datetime AND charging_kw > 0.01 AND charging_fee > 10` |

### 분석 흐름

```
PostgreSQL (charging_history, member)
    ↓  [마지막 유효 충전 < T]
침묵 밴드 코호트 구성
    ↓  [flag CSV 매칭]
플래그군 / 대조군 분리
    ↓  [복귀창 내 충전 여부]
침묵 소버킷별 복귀율 곡선 + z-test
```

---

## 파일 구조

```
chargingreturnrate/
├── charging_return_rate_v2.py  # 핵심 계산 모듈 (CLI 실행 가능)
├── app.py                      # Streamlit 대시보드
├── requirements.txt            # 의존 패키지
├── exmple.env                  # 환경변수 템플릿
└── crr.gitignore
```

---

## 설치 및 환경 설정

### 1. 패키지 설치

```bash
pip install -r requirements.txt
```

**requirements.txt**
```
pandas
sqlalchemy
psycopg2-binary
python-dotenv
streamlit
plotly
```

### 2. DB 연결 설정

루트 디렉토리에 `.env` 파일을 생성합니다. (`exmple.env` 참고)

```env
PG_DSN=postgresql+psycopg2://user:password@host:5432/dbname
```

Streamlit Cloud 배포 시에는 `.streamlit/secrets.toml`에 동일하게 설정합니다.

```toml
PG_DSN = "postgresql+psycopg2://user:password@host:5432/dbname"
```

---

## 사용법

### CLI 실행

```python
# charging_return_rate_v2.py 하단 __main__ 블록 수정 후 실행

python charging_return_rate_v2.py
```

```python
res = compute_return_rate(
    engine,
    flag_csv_path="GroupB_0609.csv",  # 플래그군 mb_id CSV
    T="2026-04-01",                   # 기준 시점
    last_charge_band=(90, 180),        # 침묵 밴드 (일)
    sub_bucket_days=30,                # 소버킷 단위 (일)
    behavior_window_days=14,           # 메타 기록용 (선택)
)
print(res.summary())
```

**출력 예시**

```
════════════════════════════════════════════════════════════
 T = 2026-04-01 | 침묵밴드 90–180일 | 복귀창 60일
 active_only=True | 행동윈도우(메타) 14일
════════════════════════════════════════════════════════════
 플래그 CSV: 818명 중 코호트 내 818명 (제외 0명)
 코호트 baseline: n=12,345명 복귀 4,812명 → 39.0%
────────────────────────────────────────────────────────────
 [전체 합산]
 플래그 47.6% (n=818)
 대조군 40.2% (n=11,527)
 gap = +7.4%p  z = 2.41  p = 0.0159  ★유의(p<0.05)
────────────────────────────────────────────────────────────
 [소버킷 곡선]
  bucket   grp     n  returners  rate
 90–120일  flag   312        159  51.0
 90–120일  ctrl  3,841      1,674  43.6
120–150일  flag   289        132  45.7
 ...
════════════════════════════════════════════════════════════
```

### Streamlit 대시보드 실행

```bash
streamlit run app.py
```

사이드바에서 파라미터를 설정하고 플래그군 CSV를 업로드하면 버킷별 복귀율 그래프와 z-test 결과를 확인할 수 있습니다.

---

## 주요 파라미터

| 파라미터 | 기본값 | 설명 |
|---|---|---|
| `T` | `오늘 - 60일` | 기준 시점. 복귀창이 오늘 이전에 닫혀야 함 |
| `last_charge_band` | `(90, 180)` | 침묵 밴드 (일). 양 끝값 포함 |
| `sub_bucket_days` | `30` | 소버킷 단위. `None`이면 단일 버킷 |
| `active_only` | `True` | `mb_status = 'active'` 필터 여부 |
| `behavior_window_days` | `None` | 플래그 CSV 생성에 사용한 행동 윈도우 (메타 기록용) |

### 플래그군 CSV 형식

```csv
mb_id
12345
67890
11111
```

헤더 유무 무관. 첫 번째 컬럼의 정수 값만 사용합니다.

---

## 통계 검증 방법

**Two-proportion z-test (pooled)**

```
H₀: 플래그군 복귀율 = 대조군 복귀율
H₁: 플래그군 복귀율 ≠ 대조군 복귀율 (양측)
```

- `scipy` 미사용 — `math.erfc`로 p값 산출
- 정규근사 조건(`np ≥ 5 AND n(1-p) ≥ 5`) 미충족 버킷은 자동으로 경고 출력

---

## v1 → v2 주요 변경 사항

| 항목 | v1 | v2 |
|---|---|---|
| 유효 충전 조건 | `pay_result = true AND charging_fee > 0` | `end_datetime > start_datetime AND charging_kw > 0.01 AND charging_fee > 10` |
| BUG-01 band_hi 경계 | `last_charge < band_hi` (T-low 당일 제외) | `last_charge <= band_hi` (포함) |
| BUG-02 import 위치 | `load_dotenv()` 이후 선언 → SyntaxError | 파일 최상단으로 이동 |
| BUG-03 소버킷 경계 | SQL band와 off-by-one | SQL과 동일한 bins 경계로 통일 |
| BUG-04 z-test 경고 | 무경고 출력 | np < 5 또는 n(1-p) < 5 버킷에 warning 플래그 |
| BUG-05 None 포맷 | `f-string` TypeError | None-safe `_fmt()` 함수로 처리 |
| BUG-06 복귀창 하드코딩 | docstring에만 "60일" 명시 | `RETURN_WINDOW_DAYS = 60` 상수로 고정 |

---

## DB 스키마 전제

```sql
-- charging_history
start_datetime  TIMESTAMP   -- KST naive (변환 금지)
end_datetime    TIMESTAMP
charging_kw     NUMERIC (또는 VARCHAR, NUMERIC 캐스트 적용)
charging_fee    NUMERIC (또는 VARCHAR, NUMERIC 캐스트 적용)
mb_id           BIGINT (또는 VARCHAR — NUMERIC 캐스트로 처리)

-- member
mb_id           BIGINT
mb_status       VARCHAR  -- 'active' 등
```

---

## 고정 가정 (변경 금지)

- 유효 충전 시점은 `start_datetime` 기준 (KST naive, timezone 변환 금지)
- 복귀 판정 범위: `start_datetime >= T AND start_datetime < T+60일`
- 침묵 일수: `T - last_charge_date` (정수, 일 단위)
- `active_only=True`이면 `mb_status = 'active'` 필터 적용
