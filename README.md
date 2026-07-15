# IL-BOT Data Studio Collector

Agibot G2 로봇의 텔레옵 데이터를 녹화하는 **수집 프로그램** (PyQt6 GUI).

노트북을 로봇과 유선 직결(노트북 `10.42.1.102/24` ↔ 로봇 `10.42.1.101`)한 상태에서 사용합니다.

## 두 저장 모드

- **서버 모드**: IL-BOT Data Studio 플랫폼으로 HTTP 업로드
- **로컬 모드**: 서버 없이 노트북에 바로 LeRobot v3 데이터셋으로 저장

GUI 상단에서 모드를 전환합니다.

## 설치

```bash
git clone https://github.com/IL-yoojihyeong/ilbot-collector.git
cd ilbot-collector
./install.sh
sudo apt install -y libxcb-cursor0    # PyQt6 런타임 
./run_gui.sh
```

첫 실행 시 **설정 창**이 자동으로 뜹니다 → 플랫폼 주소·계정(서버 모드), 로봇 IP·비밀번호 입력 →
[연결 테스트] 후 저장. 자세한 절차는 [INSTALL.md](INSTALL.md).

## 구성

```
gui/main.py          PyQt6 GUI (수집/업로드, 설정 창, 저장 모드 전환)
bridge/              수집 파이프라인 (config·api·daemon·ssh·h5_builder·pipeline·
                     local_store·robot_profile·upload_manager)
robot/               로봇측 상주 데몬·카메라 스트리머·기동 스크립트
robolabel/           포맷 컨버터 (로봇→LeRobot v3) — 로컬 저장 모드에서 사용
vendor/              GDK proto 파이썬 바인딩
scripts/             preflight_robot.py, e2e_test.py, purge_robot_records.py
```
