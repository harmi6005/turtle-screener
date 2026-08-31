name: 보유종목 추적

# ⚠️ schedule(GitHub 자체 cron) 트리거를 의도적으로 제거함.
# cron-job.org가 GitHub API로 5분마다 workflow_dispatch를 정확히 호출해주고 있는데,
# 여기에 GitHub 자체 schedule('*/5 * * * *')까지 같이 켜져 있으면 두 트리거가 각각
# 별도로 이 워크플로우를 실행시켜서 "1분에 한 번씩 오는" 것처럼 보이는 중복 실행이
# 발생했음. workflow_dispatch만 남겨서 cron-job.org의 5분 호출로만 돌게 함.
on:
  workflow_dispatch:

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: 패키지 설치
        run: pip install -r requirements.txt

      - name: 텔레그램 명령어 처리 (등록/청산/조회)
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: python scripts/telegram_listener.py

      - name: 보유종목 목표가/손절가 체크
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: python scripts/holdings_check.py

      - name: 결과 커밋
        run: |
          git config user.name "turtle-bot"
          git config user.email "turtle-bot@users.noreply.github.com"
          git add data/holdings.csv data/telegram_offset.txt
          git diff --cached --quiet || git commit -m "보유종목 상태 업데이트"
          git push
