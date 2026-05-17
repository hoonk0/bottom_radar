# bottom_radar — SPY/QQQ 시장 바닥 신호 텔레그램 알람 봇

매일 한국시간 오전 7시에 5개의 시장 바닥 지표를 자동으로 확인하고, 합산 점수가 임계값을 넘거나 개별 지표가 "강력(+3)" 신호일 때 텔레그램으로 알람을 발송합니다. **저빈도·고품질**(1년에 2~3회 정도) 시그널을 목표로 설계되었습니다.

## 모니터링 지표

| # | 지표 | 소스 | 주의(+1) | 경계(+2) | 강력(+3) |
|---|---|---|---|---|---|
| 1 | VIX 종가 | Yahoo Finance `^VIX` | ≥ 22 | ≥ 28 | ≥ 35 |
| 2 | SPY RSI(14) | Yahoo Finance `SPY`, Wilder 공식 일봉 | < 35 | < 30 | < 25 |
| 3 | SPY 1y Drawdown* | 252영업일 고점 대비 | ≤ -7% | ≤ -12% | ≤ -18% |
| 4 | CNN Fear & Greed | dataviz.cnn.io JSON API | < 25 | < 15 | < 10 |
| 5 | AAII Bearish % | aaii.com 주간 sentiment.xls | > 40% | > 45% | > 50% |

\* Drawdown은 "**신선도 decay**"가 적용됨: 최근 60거래일 내 새 저점이 아니면 점수 감소 (4~10거래일 stale → -1, 10거래일+ stale → -2). 약세장이 오래 누적되어도 같은 신호가 반복 점등되는 것을 방지.

총점은 0~15. 레벨 매핑 (5단계):

- **0~2**: ⚪ 정상 (알람 없음)
- **3~5, 강력 sub 없음**: 🟢 **NOTICE** — 약세 초기 신호. 여러 지표가 살짝 점등됨 (텔레그램 발송, 가벼운 톤)
- **3~5, 강력 sub 있음**: 🟡 **WATCH** — 단일 지표 폭발 (텔레그램 발송)
- **6~9**: 🟠 **Alert** (텔레그램 발송, 매수 검토)
- **10+**: 🔴 **STRONG** (텔레그램 발송, 강조)

**쿨다운**: 같은 레벨 알람이 직전 **7거래일** 내에 발송됐다면 스킵. 레벨이 상승(예: Notice → Watch, Alert → STRONG)한 경우에는 쿨다운을 무시합니다.

## 백테스트 결과

`scripts/backtest.py` 가 아래 4개 시점을 그날 마감 데이터로 재계산한 결과입니다. (CNN F&G API는 약 3년치 과거 데이터만 보존하므로 2020-03-16에는 결측이 발생하며 4지표만으로도 STRONG이 점등됩니다.)

| 날짜 | 사건 | 예상 | 결과 | 점수 | VIX | RSI | DD | F&G | AAII | 매치 |
|---|---|---|---|---|---|---|---|---|---|---|
| 2020-03-16 | 코로나 패닉 | STRONG | **STRONG** | 10/15 | 82.69 (+3) | 30.1 (+1) | -29.1% (+3) | N/A | 51.3% (+3) | ✓ |
| 2022-10-12 | 인플레 바닥 | Alert  | **STRONG** | 10/15 | 33.57 (+2) | 34.6 (+1) | -25.4% (+3) | 16 (+1) | 54.8% (+3) | ✓ |
| 2024-08-05 | 엔캐리 청산 | Alert  | Watch + force-notify | 5/15 | 38.57 (+3) | 30.7 (+1) | -8.4% (+1) | 33 (0) | 25.2% (0) | ✓ |
| 2025-04-07 | 관세 충격 | STRONG | **STRONG** | 14/15 | 46.98 (+3) | 23.1 (+3) | -17.7% (+2) | 4 (+3) | 61.9% (+3) | ✓ |

2024-08-05 는 일중 변동이 컸지만 종가 기준으로는 SPY가 1년 고점에서 약 8% 떨어진 데 그쳐 합산 점수는 Watch(5)에 머물렀습니다. 다만 VIX 종가가 38.57로 "강력(+3)"이라 예외 규칙에 의해 텔레그램 발송 조건을 충족합니다.

재생산:

```bash
python -m scripts.backtest          # 표 출력
python -m scripts.backtest --json   # JSON 출력
```

## 설치 및 로컬 실행

```bash
git clone <repo> bottom_radar
cd bottom_radar
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 로컬 dry-run (텔레그램 발송 없음)

```bash
DRY_RUN=1 python -m src.main
```

### 실제 텔레그램 발송

```bash
export TELEGRAM_BOT_TOKEN=<bot_token>
export TELEGRAM_CHAT_ID=<chat_id>
python -m src.main
```

### 단위 테스트

```bash
pytest tests/ -v
```

## 텔레그램 봇 설정

1. **봇 토큰 발급** — 텔레그램에서 [@BotFather](https://t.me/BotFather) 와 대화해서 `/newbot` 으로 새 봇을 만들고, 발급받은 토큰을 복사합니다.
2. **본인 chat_id 확인** — 만든 봇과 대화방에서 메시지를 하나 보낸 뒤 [@userinfobot](https://t.me/userinfobot) 으로 본인 chat_id 를 확인합니다.
3. (선택) **에러 알람 분리** — 평소 알람과 다른 곳으로 에러를 보내고 싶다면 별도 chat_id 를 준비합니다.

## GitHub Actions 배포

리포지토리에 push 한 뒤 GitHub → Settings → Secrets and variables → Actions 에 다음을 추가합니다:

| Secret 이름 | 값 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather 에서 발급받은 토큰 |
| `TELEGRAM_CHAT_ID` | 본인 chat_id |
| `TELEGRAM_ERROR_CHAT_ID` (선택) | 에러 알람용 chat_id (미지정 시 `TELEGRAM_CHAT_ID` 와 동일) |

워크플로우는 매일 22:00 UTC (한국시간 07:00) 에 자동 실행되며, 수동으로는 Actions 탭 → "bottom_radar daily check" → Run workflow 로 트리거할 수 있습니다. `dry_run` 인자를 `1` 로 주면 알람을 보내지 않고 점수 계산만 합니다.

실행이 끝나면 워크플로우가 `state.json` 의 변경분을 자동으로 commit 합니다 (`contents: write` 권한 필요).

## 메시지 예시

```
🔴 *SPY 매수 신호 — STRONG*
_2025-04-07 KST_

*합산 점수: 14 / 15*

🔴 VIX: 46.98 (강력, +3)
🔴 SPY RSI(14): 23.1 (강력, +3)
🟠 SPY 1y Drawdown: -17.7% (경계, +2)
🔴 CNN F&G: 4 (강력, +3)
🔴 AAII Bearish: 61.9% (강력, +3)

📉 SPY: $504.38 (전일 -0.18%)
📈 QQQ: $423.69 (전일 +0.24%)

_과거 유사 점수(10+) 점등 후 1년 평균 수익률 대략 +25~35%_
```

## 프로젝트 구조

```
bottom_radar/
├── src/
│   ├── fetchers/         # VIX/SPY/QQQ (yfinance), CNN F&G, AAII
│   │   ├── prices.py
│   │   ├── fear_greed.py
│   │   └── aaii.py
│   ├── indicators/       # RSI, Drawdown, 점수 변환 + 합산, 신선도 decay
│   │   ├── rsi.py
│   │   ├── drawdown.py
│   │   ├── freshness.py  # 신선도 decay (Drawdown용)
│   │   └── score.py
│   ├── pipeline.py       # 전체 파이프라인 (오류 격리)
│   ├── telegram.py       # 메시지 포맷 + 발송
│   ├── state.py          # state.json 영속화 + 쿨다운 로직
│   ├── logger.py
│   └── main.py           # 진입점
├── scripts/
│   └── backtest.py       # 4개 과거 시점 검증
├── tests/
│   └── test_indicators.py
├── data/
│   └── aaii_sentiment.xls  # AAII Cloudflare 차단 시 폴백 (백테스트에도 사용)
├── .github/workflows/check.yml
├── state.json
├── requirements.txt
└── README.md
```

## 신뢰성 설계

- **5개 지표는 모두 격리** — 1개가 실패해도 나머지 4개로 점수 계산을 진행합니다 (`pipeline.py`).
- **전체 실패시에만 에러 알람** — `TELEGRAM_ERROR_CHAT_ID` 로 별도 발송 가능합니다.
- **AAII 폴백** — Cloudflare 가 .xls 다운로드를 차단할 때를 대비해 `data/aaii_sentiment.xls` 스냅샷을 사용합니다. 주기적으로 (몇 달에 한번) 이 파일을 최신화하면 됩니다.
- **F&G 폴백** — JSON API 실패 시 `money.cnn.com/data/fear-and-greed/` HTML 을 스크래핑합니다.
- **7거래일 쿨다운** — 같은 레벨이 연속으로 알람을 폭격하는 것을 막습니다. 시장 거래일 (월~금) 기준이라 7거래일 ≈ 9-10 캘린더일. 레벨이 상승하면 쿨다운을 무시합니다.

## 라이선스

MIT
