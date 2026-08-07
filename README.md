# 터틀 트레이딩 자동 스크리너 (GitHub Actions)

폰이나 PC를 켜놓지 않아도, 정해진 시간에 자동으로 전체 스캔하고
5분마다 관심종목을 재확인해서 "확정" 전환 시 텔레그램으로 알림을 보내줍니다.

## 폴더 구조
```
turtle_github/
├── index.html                 # 웹 대시보드 (홈화면 추가용)
├── common.py                  # 공통 로직 (ATR, 터틀 판정, 텔레그램 알림)
├── requirements.txt           # 필요 패키지
├── data/                      # 스캔 결과 CSV가 저장되는 곳
├── scripts/
│   ├── full_scan_korea.py     # 국내 전체 스캔
│   ├── full_scan_us.py        # 미국 전체 스캔
│   ├── full_scan_bithumb.py   # 빗썸 전체 스캔
│   ├── recheck_korea.py       # 국내 관심종목 재확인
│   ├── recheck_us.py          # 미국 관심종목 재확인
│   └── recheck_bithumb.py     # 빗썸 관심코인 재확인
└── .github/workflows/         # 자동 실행 스케줄 설정
    ├── full_scan_korea.yml    # 매일 저녁 8시(KST) 자동 실행
    ├── full_scan_us.yml       # 매일 아침 8시(KST) 자동 실행
    ├── full_scan_bithumb.yml  # 매일 자정 30분(KST) 자동 실행
    ├── recheck.yml            # 5분마다 관심종목 재확인
    └── pages.yml              # 웹 대시보드 자동 배포
```

## 설정 방법 (처음 1번만)

### 1단계: GitHub 저장소(repository) 만들기
1. https://github.com 가입 (이미 있으면 스킵)
2. 우측 상단 "+" → "New repository" 클릭
3. 이름 아무거나 (예: `turtle-screener`) → **Public**으로 설정 (Actions 무료 무제한 사용 위해 권장)
4. "Create repository" 클릭

### 2단계: 이 폴더 전체를 저장소에 업로드
- 컴퓨터에서 GitHub 데스크톱 앱을 쓰거나, 웹에서 "Add file → Upload files"로
  이 `turtle_github` 폴더 안의 모든 파일/폴더를 그대로 올려주세요
  (`.github` 폴더도 반드시 포함해야 자동 실행 설정이 적용돼요)

### 3단계: (선택) 텔레그램 알림 설정
확정 전환 종목이 나올 때 텔레그램으로 알림 받고 싶으면:
1. 텔레그램에서 "BotFather" 검색 → `/newbot` 명령으로 봇 생성 → **토큰(TELEGRAM_BOT_TOKEN)** 받기
2. 만든 봇과 대화 시작 후, `https://api.telegram.org/bot<토큰>/getUpdates` 접속해서 **chat_id** 확인
3. GitHub 저장소 → Settings → Secrets and variables → Actions → "New repository secret"
   - `TELEGRAM_BOT_TOKEN` 등록
   - `TELEGRAM_CHAT_ID` 등록

텔레그램 설정을 안 해도 스크립트는 정상 작동해요 (알림만 안 옴, 결과는 CSV로 계속 쌓임).

### 4단계: Actions 활성화 확인
- 저장소 상단 메뉴 "Actions" 탭 클릭 → 워크플로우들이 보이면 자동으로 활성화된 상태예요
- 처음엔 "Actions 사용 허가" 버튼이 뜰 수 있는데 클릭해서 허용해주세요

### 5단계: 테스트 (수동 실행)
- Actions 탭 → 원하는 워크플로우 클릭 (예: "국내 전체스캔")
- 우측 "Run workflow" 버튼으로 즉시 1번 실행해서 정상 작동하는지 확인

### 6단계: 웹 대시보드(앱처럼 쓰기) 켜기
1. 저장소 → Settings → Pages
2. "Build and deployment" → Source를 **"GitHub Actions"** 로 선택
3. 저장 후 Actions 탭에서 "Pages 배포" 워크플로우가 자동으로 1번 실행됨
4. 완료되면 Settings → Pages 상단에 뜨는 주소로 접속
   (보통 `https://<깃허브아이디>.github.io/<저장소이름>/` 형태)

**폰 홈 화면에 추가하기**
- 아이폰(사파리): 주소 접속 → 공유 버튼 → "홈 화면에 추가"
- 안드로이드(크롬): 주소 접속 → 우측 상단 점 3개 메뉴 → "홈 화면에 추가" 또는 "앱 설치"
- 이렇게 하면 아이콘이 생겨서 앱처럼 눌러서 바로 켤 수 있어요

대시보드는 국내/미국/빗썸 탭으로 나뉘고, 각 System1/System2 별로
확정유지 / 확정이탈 / 확정 / 관심 종목을 색으로 구분해서 보여줘요.
1분마다 자동으로 새로고침돼요.

## 결과 확인
- `data/turtle_korea_result.csv` 등 파일이 저장소에 자동으로 커밋되면서 계속 갱신돼요
- GitHub 웹사이트에서 그냥 파일 열어서 표 형태로 바로 볼 수 있어요
- 다운로드하면 엑셀로도 열려요

## 스케줄 커스텀
- 각 `.github/workflows/*.yml` 파일 안의 `cron` 값을 수정하면 시간 변경 가능해요
- cron은 UTC 기준이라 KST(한국시간) = UTC + 9시간이에요
- 예: 국내 스캔을 저녁 7시로 바꾸려면 `cron: '0 10 * * *'` (19:00 KST = 10:00 UTC)

## 주의사항
- Public 저장소면 GitHub Actions 무료 무제한이지만, Private 저장소는 월 2,000분 제한이 있어요
  (5분마다 재확인 도는 건 분량이 꽤 되니 Public 권장)
- 5분 간격은 GitHub 스케줄러 특성상 실제로는 몇 분씩 밀려서 실행될 수 있어요 (정확히 5분마다 보장 X)
- 저장소에 60일간 커밋이 없으면 GitHub이 자동으로 스케줄을 비활성화해요 (재확인 워크플로우가
  계속 커밋을 만들기 때문에 이 문제는 거의 발생 안 해요)
