# IL-BOT Data Studio Collector 설치 

G2 로봇 데이터를 녹화하는 수집 GUI. 

## requirements

- Ubuntu 22.04+ (PyQt6 GUI), 유선 랜 포트(로봇 직결), 디스크 여유 20GB+
- 플랫폼 주소·계정 (서버 모드일 때 — 플랫폼 설치 시 정한 `ROBOLABEL_PASSWORD`)
- 시스템 의존성: `uv`(install.sh가 설치), OpenSSH 클라이언트, Qt 런타임 보조 라이브러리
  (`sudo apt install libxcb-cursor0`).
  
## install

```bash
git clone https://github.com/IL-yoojihyeong/ilbot-collector.git
cd ilbot-collector && ./install.sh
sudo apt install -y libxcb-cursor0        # PyQt6 런타임 (한 번만)
```

## setup

`./run_gui.sh` 첫 실행 시 **설정 창이 자동으로 뜬다** → 플랫폼 주소·계정,
로봇 IP·비밀번호를 입력하고 [연결 테스트] 후 저장하면 `config.json`이 생성된다.
JSON을 직접 편집할 필요가 없다. (나중에 바꾸려면 GUI [⚙ 설정])

## 로봇 연결 → 검증 → 사용

```bash
# 노트북 유선 IP를 10.42.1.102/24 로 설정 (로봇ip = 10.42.1.101)
.venv/bin/python scripts/preflight_robot.py    # GDK/proto/카메라 일괄 점검
./run_gui.sh                                   # 수집 GUI
```

## 로컬 저장 모드 (서버 없이)

플랫폼 서버가 없어도 GUI 상단 **저장 모드 → "로컬 저장 (LeRobot)"**을 선택하면
노트북에 바로 LeRobot v3 데이터셋으로 저장된다 (`config.json`의 `mode: "local"`과 동일):

- **데이터셋 이름**을 정하면 여러 세션에 걸쳐 같은 데이터셋에 에피소드가 **append 누적**되고,
  에피소드마다 meta가 갱신되어 언제 중단해도 유효한 데이터셋 상태를 유지한다
- 산출 구조: `~/ilbot_data/datasets/<이름>/`(LeRobot v3, 서버 변환과 동일 포맷),
  `~/ilbot_data/raw/<uuid>/`(40토픽+카메라 원본 — `local.keep_raw: false`로 끌 수 있음)
- Task instruction은 GUI의 Task 입력란 텍스트가 에피소드 task로 기록된다
- **변환본→raw 역추적**: 각 에피소드의 `source_path`(meta/episodes parquet)가 변환에 쓴
  raw 디렉토리를 영구 기록 — GUI [에피소드 목록] 창에서 확인·열기 가능
- 나중에 플랫폼이 생기면: 데이터셋 폴더를 경로 import 하거나 raw를 HTTP 업로드하면
  그대로 등록된다
