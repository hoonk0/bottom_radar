# Pi 배포 가이드

## 1. 매일 알람 (cron — 기존 설정 유지)

매일 07:00 KST 자동 실행. `crontab -e` 에 등록:

```
0 7 * * *   cd /home/kyunghoon/bottom_radar && set -a && . ./.env && set +a && /home/kyunghoon/bottom_radar/.venv/bin/python -m src.main >> /home/kyunghoon/bottom_radar/cron.log 2>&1
```

## 2. 인터랙티브 봇 (systemd 서비스 — 신규)

24시간 떠있으면서 텔레그램의 `/help`, `/year`, `/indicator`, `/status` 명령 처리.

### 설치

```bash
sudo cp /home/kyunghoon/bottom_radar/deploy/bottom_radar_bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable bottom_radar_bot
sudo systemctl start bottom_radar_bot
```

### 상태 확인

```bash
sudo systemctl status bottom_radar_bot
tail -f ~/bottom_radar/bot.log
```

봇 가동 메시지("🤖 bottom_radar bot 가동") 가 텔레그램으로 도착하면 OK.

### 명령어 등록 (BotFather)

봇이 자동완성 메뉴를 보여주게 하려면 [@BotFather](https://t.me/BotFather) 와 대화:

```
/setcommands → 봇 선택 → 다음 텍스트 붙여넣기

help - 사용 가능 명령어
status - 오늘 5지표 점수
year - 연도별 강력 발화 + 1년 수익률 (예: /year 2022)
indicator - 지표별 역사 발화 (vix, rsi, drawdown, fear_greed, aaii)
about - 봇 소개
```

### 중지/재시작

```bash
sudo systemctl stop bottom_radar_bot          # 일시 중지
sudo systemctl restart bottom_radar_bot       # 재시작 (코드 업데이트 후)
sudo systemctl disable bottom_radar_bot       # 자동 시작 해제
```

### 코드 업데이트 시

```bash
cd ~/bottom_radar
git pull
sudo systemctl restart bottom_radar_bot
```

## 자원 사용

- cron 작업: 매일 1~2분, 평소 0%
- 봇: 항상 떠있지만 long-polling 대기 중 RAM 60~80MB, CPU 거의 0%

Pi 4 (4GB) 에서는 무한매수/공모주와 같이 돌려도 충분합니다.

## 로그 관리

`bot.log` 가 계속 누적되니 가끔 정리:

```bash
> ~/bottom_radar/bot.log   # 초기화
> ~/bottom_radar/cron.log  # 초기화
```

또는 logrotate 설정 (선택사항).
