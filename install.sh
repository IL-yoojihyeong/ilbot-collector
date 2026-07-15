#!/usr/bin/env bash
# IL-BOT Data Studio Collector 설치 (Linux 노트북, 자립형 — 이 레포만 있으면 됨)
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

uv sync   # 번들 컨버터(robolabel/)는 레포에 포함 — 진입점 sys.path로 로드됨

cat <<'MSG'

설치 완료.

  ./run_gui.sh    # 첫 실행 시 설정 창이 떠서 플랫폼 주소·계정, 로봇 IP·비밀번호 입력
                  # (서버 없이 노트북에만 저장하려면 GUI에서 '로컬 저장' 모드 선택)

  # Qt 런타임 라이브러리가 없다는 오류가 나면 한 번만:
  sudo apt install -y libxcb-cursor0

로봇 연결 후 점검(선택):
  .venv/bin/python scripts/preflight_robot.py
MSG
