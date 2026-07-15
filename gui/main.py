#!/usr/bin/env python3
"""RoboLabel Bridge GUI — G2 텔레옵 데이터 수집기 (PyQt6).

좌측 사이드바: [수집] 카메라 스트림·카운트다운·녹화 제어 / [업로드] 백그라운드 처리 상태.
로봇에는 bridge_daemon(워밍업 상주)이 배포되어 시작/종료가 즉시 반영된다.
실행: ./run_gui.sh
"""
import sys
import time
import traceback
import uuid as uuidlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtCore import (QObject, QRunnable, Qt, QThread, QThreadPool, QTimer,
                          QUrl, pyqtSignal)
from PyQt6.QtGui import QColor, QDesktopServices, QFont, QImage, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QGridLayout, QGroupBox,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QMainWindow,
    QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QStackedWidget,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget, QHeaderView,
)

from bridge import config as cfgmod
from bridge import local_store, pipeline
from bridge.api_client import RoboLabelAPI
from bridge.daemon_client import DaemonClient
from bridge.upload_manager import UploadManager

DOT = {"ok": "🟢", "bad": "🔴", "unknown": "⚪"}
STEP_ICON = {"idle": "☐", "run": "⏳", "ok": "✅", "fail": "❌"}


class Signals(QObject):
    done = pyqtSignal(object)
    error = pyqtSignal(str)


class Worker(QRunnable):
    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn, self.args, self.kwargs = fn, args, kwargs
        self.signals = Signals()

    def run(self):
        try:
            self.signals.done.emit(self.fn(*self.args, **self.kwargs))
        except Exception as e:
            self.signals.error.emit(f"{e}\n{traceback.format_exc(limit=2)}")


class StreamThread(QThread):
    """Polls the daemon's /frame.jpg for each camera at low fps while enabled."""
    frames = pyqtSignal(dict)          # {cam_name: jpeg_bytes}

    def __init__(self, daemon: DaemonClient, cams: list, fps: float):
        super().__init__()
        self.daemon = daemon
        self.cams = cams
        self.interval = 1.0 / max(fps, 0.5)
        self.enabled = False
        self._quit = False

    def run(self):
        while not self._quit:
            if not self.enabled:
                time.sleep(0.3)
                continue
            t0 = time.time()
            out = {}
            for c in self.cams:
                data = self.daemon.get_frame(c, timeout=2.0)
                if data:
                    out[c] = data
            if out:
                self.frames.emit(out)
            dt = time.time() - t0
            if dt < self.interval:
                time.sleep(self.interval - dt)

    def stop(self):
        self._quit = True


class SettingsDialog(QDialog):
    """필수 접속 설정만 담은 창 — 저장 시 config.json 기록 (없으면 생성).

    다른 회사 노트북 설치 시 JSON 편집 없이 이 창의 4칸이 설정의 전부:
    플랫폼 주소/계정(서버 모드용) + 로봇 IP/비밀번호.
    """

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("설정 — IL-BOT Data Studio Bridge")
        self.setMinimumWidth(560)
        g = QGridLayout(self)

        g.addWidget(QLabel("<b>플랫폼 (서버 모드에서만 필요)</b>"), 0, 0, 1, 3)
        g.addWidget(QLabel("플랫폼 주소"), 1, 0)
        self.edt_url = QLineEdit(cfg.server.api_url)
        self.edt_url.setPlaceholderText("http://<워크스테이션 IP>:8322")
        g.addWidget(self.edt_url, 1, 1)
        self.btn_test_srv = QPushButton("연결 테스트")
        self.btn_test_srv.clicked.connect(self.on_test_server)
        g.addWidget(self.btn_test_srv, 1, 2)
        g.addWidget(QLabel("계정 / 비밀번호"), 2, 0)
        row = QHBoxLayout()
        self.edt_user = QLineEdit(cfg.server.api_user)
        self.edt_user.setPlaceholderText("admin")
        self.edt_pw = QLineEdit(cfg.server.api_password)
        self.edt_pw.setPlaceholderText("ROBOLABEL_PASSWORD와 동일 (없으면 비움)")
        self.edt_pw.setEchoMode(QLineEdit.EchoMode.Password)
        row.addWidget(self.edt_user, 1)
        row.addWidget(self.edt_pw, 2)
        g.addLayout(row, 2, 1, 1, 2)

        g.addWidget(QLabel("<b>로봇 (유선 직결 — 노트북 IP를 10.42.1.102/24로 설정)</b>"),
                    3, 0, 1, 3)
        g.addWidget(QLabel("로봇 IP / 계정"), 4, 0)
        row2 = QHBoxLayout()
        self.edt_rhost = QLineEdit(cfg.robot.ssh_host)
        self.edt_ruser = QLineEdit(cfg.robot.ssh_user)
        self.edt_ruser.setFixedWidth(90)
        row2.addWidget(self.edt_rhost, 1)
        row2.addWidget(self.edt_ruser)
        g.addLayout(row2, 4, 1)
        self.btn_test_rob = QPushButton("연결 테스트")
        self.btn_test_rob.clicked.connect(self.on_test_robot)
        g.addWidget(self.btn_test_rob, 4, 2)
        g.addWidget(QLabel("로봇 비밀번호"), 5, 0)
        self.edt_rpw = QLineEdit(cfg.robot.ssh_password)
        self.edt_rpw.setEchoMode(QLineEdit.EchoMode.Password)
        g.addWidget(self.edt_rpw, 5, 1, 1, 2)

        g.addWidget(QLabel("기본 저장 모드"), 6, 0)
        self.cmb_mode = QComboBox()
        self.cmb_mode.addItem("서버 업로드", "server")
        self.cmb_mode.addItem("로컬 저장 (LeRobot)", "local")
        self.cmb_mode.setCurrentIndex(1 if cfg.mode == "local" else 0)
        g.addWidget(self.cmb_mode, 6, 1)

        self.lbl_status = QLabel("")
        g.addWidget(self.lbl_status, 7, 0, 1, 3)
        h = QHBoxLayout()
        h.addStretch(1)
        btn_cancel = QPushButton("취소")
        btn_cancel.clicked.connect(self.reject)
        btn_save = QPushButton("저장")
        btn_save.setDefault(True)
        btn_save.clicked.connect(self.on_save)
        h.addWidget(btn_cancel)
        h.addWidget(btn_save)
        g.addLayout(h, 8, 0, 1, 3)

    def _collect(self):
        self.cfg.server.api_url = self.edt_url.text().strip().rstrip("/")
        self.cfg.server.api_user = self.edt_user.text().strip()
        self.cfg.server.api_password = self.edt_pw.text()
        self.cfg.server.transport = "http"        # 신규 설치는 HTTP 업로드 표준
        self.cfg.robot.ssh_host = self.edt_rhost.text().strip()
        self.cfg.robot.ssh_user = self.edt_ruser.text().strip() or "agi"
        self.cfg.robot.ssh_password = self.edt_rpw.text()
        self.cfg.mode = self.cmb_mode.currentData()

    def on_test_server(self):
        self._collect()
        from bridge.api_client import RoboLabelAPI
        ok = RoboLabelAPI(self.cfg.server).ping()
        self.lbl_status.setText("🟢 플랫폼 연결 성공" if ok
                                else "🔴 플랫폼 연결 실패 — 주소/계정/방화벽 확인")

    def on_test_robot(self):
        self._collect()
        from bridge.ssh_link import SSHSession
        try:
            SSHSession(self.cfg.robot.ssh_host, self.cfg.robot.ssh_user,
                       self.cfg.robot.ssh_password, timeout=6).close()
            self.lbl_status.setText("🟢 로봇 SSH 연결 성공")
        except Exception as e:
            self.lbl_status.setText(f"🔴 로봇 연결 실패: {str(e)[:60]}")

    def on_save(self):
        self._collect()
        cfgmod.save_settings(self.cfg)
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = cfgmod.load()
        self.pool = QThreadPool()
        self.api = RoboLabelAPI(self.cfg.server)
        self.daemon = DaemonClient(self.cfg.robot, self.cfg.daemon.port)
        self.uploads = UploadManager(self.cfg)
        self.uploads.jobs_changed.connect(self.refresh_upload_table)
        self.uploads.log.connect(self.log)

        self.state = "idle"        # idle|counting|starting|recording|stopping
        self.daemon_up = False
        self.rec_uuid = None
        self.rec_t0 = None
        self.countdown_left = 0
        self.last_frames = {}

        self.setWindowTitle(
            f"IL-BOT Data Studio Bridge v{cfgmod.BRIDGE_VERSION} — G2 데이터 수집")
        self.setMinimumSize(920, 720)
        self._first_run = not cfgmod.config_exists()
        self._build_ui()
        self._apply_mode(initial=True)

        self.stream = StreamThread(self.daemon, self.cfg.daemon.stream_cams,
                                   self.cfg.daemon.stream_fps)
        self.stream.frames.connect(self.on_frames)
        self.stream.start()

        self.ui_timer = QTimer(self, interval=500, timeout=self._tick)
        self.ui_timer.start()
        self.hb_timer = QTimer(self, interval=20000, timeout=self._heartbeat)
        self.hb_timer.start()
        self.status_timer = QTimer(self, interval=5000, timeout=self._poll_daemon)
        self.status_timer.start()
        self.cd_timer = QTimer(self, interval=1000, timeout=self._countdown_tick)

        if self._first_run:
            QTimer.singleShot(300, self._first_run_setup)
        self.check_connections()
        self.refresh_targets()
        self._poll_daemon()

    def _first_run_setup(self):
        QMessageBox.information(
            self, "환영합니다",
            "설정 파일이 없어 초기 설정을 시작합니다.\n"
            "플랫폼 주소·계정과 로봇 접속 정보를 입력하세요.")
        self.open_settings()

    def open_settings(self):
        dlg = SettingsDialog(self.cfg, self)
        if dlg.exec():
            # 접속 정보가 바뀌었으니 클라이언트 재생성 + 화면 갱신
            self.api = RoboLabelAPI(self.cfg.server)
            self.cmb_mode.setCurrentIndex(1 if self.cfg.mode == "local" else 0)
            self._apply_mode()
            self.log("설정 저장됨")

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        root = QWidget()
        h = QHBoxLayout(root)

        self.sidebar = QListWidget()
        self.sidebar.addItems(["🎥  수집", "⇪  업로드"])
        self.sidebar.setFixedWidth(110)
        self.sidebar.setCurrentRow(0)
        self.sidebar.currentRowChanged.connect(lambda i: self.pages.setCurrentIndex(i))
        h.addWidget(self.sidebar)

        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_collect_page())
        self.pages.addWidget(self._build_upload_page())
        h.addWidget(self.pages, 1)
        self.setCentralWidget(root)

    def _build_collect_page(self):
        page = QWidget()
        v = QVBoxLayout(page)

        # 연결 / 워밍업
        conn = QGroupBox("연결 · 워밍업")
        ch = QHBoxLayout(conn)
        self.lbl_server = QLabel(f"{DOT['unknown']} 서버")
        self.lbl_robot = QLabel(f"{DOT['unknown']} 로봇")
        self.lbl_daemon = QLabel(f"{DOT['unknown']} 데몬")
        self.btn_warm = QPushButton("🔥 워밍업 시작")
        self.btn_warm.clicked.connect(self.on_warmup_clicked)
        btn_chk = QPushButton("연결 확인")
        btn_chk.clicked.connect(self.check_connections)
        btn_settings = QPushButton("⚙ 설정")
        btn_settings.clicked.connect(self.open_settings)
        for w in (self.lbl_server, self.lbl_robot, self.lbl_daemon):
            ch.addWidget(w)
        ch.addStretch(1)
        ch.addWidget(btn_settings)
        ch.addWidget(btn_chk)
        ch.addWidget(self.btn_warm)
        v.addWidget(conn)

        # 스트림: head(메인) + 손목 2개
        sh = QHBoxLayout()
        self.view = QLabel("워밍업을 시작하면\n카메라 스트림이 표시됩니다")
        self.view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.view.setStyleSheet("background:#111;color:#888")
        self.view.setMinimumSize(480, 330)
        sh.addWidget(self.view, 5)
        side = QVBoxLayout()
        self.hand_views = {}
        for cam, title in (("hand_left_color", "왼손"), ("hand_right_color", "오른손")):
            lbl = QLabel(title)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet("background:#111;color:#888")
            lbl.setMinimumSize(220, 150)
            self.hand_views[cam] = lbl
            side.addWidget(lbl, 1)
        sh.addLayout(side, 2)
        v.addLayout(sh, 1)

        # 대상 + 카운트다운
        tgt = QGroupBox("수집 대상")
        g = QGridLayout(tgt)
        g.addWidget(QLabel("저장 모드"), 0, 0)
        self.cmb_mode = QComboBox()
        self.cmb_mode.addItem("서버 업로드", "server")
        self.cmb_mode.addItem("로컬 저장 (LeRobot)", "local")
        self.cmb_mode.setCurrentIndex(1 if self.cfg.mode == "local" else 0)
        self.cmb_mode.currentIndexChanged.connect(lambda _i: self._apply_mode())
        g.addWidget(self.cmb_mode, 0, 1)
        btn_rf = QPushButton("새로고침")
        btn_rf.clicked.connect(self.refresh_targets)
        g.addWidget(btn_rf, 0, 4)

        # 서버 모드: 프로젝트/Job
        self.lbl_project = QLabel("프로젝트")
        g.addWidget(self.lbl_project, 1, 0)
        self.cmb_project = QComboBox()
        self.cmb_project.currentIndexChanged.connect(self.on_project_changed)
        g.addWidget(self.cmb_project, 1, 1)
        self.lbl_job = QLabel("Job")
        g.addWidget(self.lbl_job, 2, 0)
        self.cmb_job = QComboBox()
        g.addWidget(self.cmb_job, 2, 1)

        # 로컬 모드: 데이터셋(누적 대상) — 서버 위젯과 같은 칸을 쓰고 가시성으로 전환
        self.lbl_dataset = QLabel("데이터셋")
        g.addWidget(self.lbl_dataset, 1, 0)
        self.cmb_dataset = QComboBox()
        self.cmb_dataset.setEditable(True)
        self.cmb_dataset.lineEdit().setPlaceholderText("누적할 데이터셋 이름 (예: g2-demo)")
        self.cmb_dataset.currentTextChanged.connect(self._update_dataset_info)
        g.addWidget(self.cmb_dataset, 1, 1)
        self.btn_ds_new = QPushButton("＋ 새 데이터셋")
        self.btn_ds_new.clicked.connect(self.on_new_dataset)
        g.addWidget(self.btn_ds_new, 1, 2)
        self.btn_ds_eps = QPushButton("에피소드 목록")
        self.btn_ds_eps.clicked.connect(self.on_show_episodes)
        g.addWidget(self.btn_ds_eps, 1, 3)
        self.ds_dir_row = QWidget()
        rh = QHBoxLayout(self.ds_dir_row)
        rh.setContentsMargins(0, 0, 0, 0)
        self.lbl_ds_info = QLabel("")
        self.btn_ds_dir = QPushButton("저장위치 변경")
        self.btn_ds_dir.clicked.connect(self.on_change_data_dir)
        rh.addWidget(self.lbl_ds_info)
        rh.addWidget(self.btn_ds_dir)
        rh.addStretch(1)
        g.addWidget(self.ds_dir_row, 2, 0, 1, 2)

        g.addWidget(QLabel("카운트다운"), 2, 2)
        self.spn_cd = QSpinBox()
        self.spn_cd.setRange(0, 10)
        self.spn_cd.setValue(self.cfg.recording.countdown_s)
        self.spn_cd.setSuffix(" 초")
        g.addWidget(self.spn_cd, 2, 3)
        self.lbl_desc = QLabel("설명")
        g.addWidget(self.lbl_desc, 3, 0)
        self.edt_desc = QLineEdit()
        self.edt_desc.setPlaceholderText("에피소드 설명 (예: pick up the bottle)")
        g.addWidget(self.edt_desc, 3, 1, 1, 4)
        g.setColumnStretch(1, 1)
        v.addWidget(tgt)

        # 녹화
        rh = QHBoxLayout()
        self.btn_rec = QPushButton("●  녹화 시작")
        self.btn_rec.setMinimumHeight(56)
        f = QFont()
        f.setPointSize(15)
        f.setBold(True)
        self.btn_rec.setFont(f)
        self.btn_rec.clicked.connect(self.on_record_clicked)
        self.lbl_elapsed = QLabel("00:00")
        self.lbl_elapsed.setFont(f)
        rh.addWidget(self.btn_rec, 1)
        rh.addWidget(self.lbl_elapsed)
        v.addLayout(rh)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setMaximumHeight(130)
        v.addWidget(self.log_view)
        return page

    def _build_upload_page(self):
        page = QWidget()
        v = QVBoxLayout(page)
        top = QHBoxLayout()
        self.lbl_upload_summary = QLabel("업로드 대기 0건")
        btn_retry = QPushButton("실패 재시도")
        btn_retry.clicked.connect(self.on_retry_failed)
        btn_clear = QPushButton("완료 항목 정리")
        btn_clear.clicked.connect(self.uploads.clear_done)
        top.addWidget(self.lbl_upload_summary)
        top.addStretch(1)
        top.addWidget(btn_retry)
        top.addWidget(btn_clear)
        v.addLayout(top)

        self.tbl = QTableWidget(0, 6)
        self.tbl.setHorizontalHeaderLabels(["시각", "UUID", "대상", "길이", "진행 단계", "상태"])
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.tbl.horizontalHeader().setStretchLastSection(True)
        self.tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        v.addWidget(self.tbl, 1)
        self.refresh_upload_table()
        return page

    # ------------------------------------------------------------- helpers
    def log(self, msg):
        self.log_view.appendPlainText(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def _spawn(self, fn, *args, done=None, error=None, **kwargs):
        w = Worker(fn, *args, **kwargs)
        if done:
            w.signals.done.connect(done)
        w.signals.error.connect(error or (lambda m: self.log("오류: " + m.splitlines()[0])))
        self.pool.start(w)

    def _tick(self):
        if self.state == "recording" and self.rec_t0:
            s = int(time.time() - self.rec_t0)
            self.lbl_elapsed.setText(f"{s // 60:02d}:{s % 60:02d}")

    def _set_state(self, state):
        self.state = state
        for w in (self.cmb_project, self.cmb_job, self.cmb_dataset, self.cmb_mode):
            w.setEnabled(state == "idle")
        style_rec = "background:#c62828;color:white"
        if state == "idle":
            self.btn_rec.setText("●  녹화 시작")
            self.btn_rec.setStyleSheet("")
            self.btn_rec.setEnabled(True)
        elif state == "counting":
            self.btn_rec.setText("✕  카운트다운 취소")
            self.btn_rec.setStyleSheet("background:#ef6c00;color:white")
        elif state == "starting":
            self.btn_rec.setText("… 시작 중")
            self.btn_rec.setEnabled(False)
        elif state == "recording":
            self.btn_rec.setText("■  녹화 종료")
            self.btn_rec.setStyleSheet(style_rec)
            self.btn_rec.setEnabled(True)
        elif state == "stopping":
            self.btn_rec.setText("… 종료 중")
            self.btn_rec.setEnabled(False)

    # -------------------------------------------------------------- stream
    def on_frames(self, frames: dict):
        self.last_frames.update(frames)
        self._paint_view()
        for cam, lbl in self.hand_views.items():
            data = self.last_frames.get(cam)
            if data:
                pix = QPixmap.fromImage(QImage.fromData(data)).scaled(
                    lbl.width(), lbl.height(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
                lbl.setPixmap(pix)

    def _paint_view(self):
        data = self.last_frames.get(self.cfg.daemon.stream_cams[0])
        if not data:
            return
        img = QImage.fromData(data)
        pix = QPixmap.fromImage(img).scaled(
            self.view.width(), self.view.height(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        p = QPainter(pix)
        if self.state == "counting":
            p.setPen(QColor(255, 255, 255))
            f = QFont()
            f.setPointSize(110)
            f.setBold(True)
            p.setFont(f)
            p.fillRect(pix.rect(), QColor(0, 0, 0, 90))
            p.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, str(self.countdown_left))
        elif self.state == "recording":
            p.setPen(QColor(255, 60, 60))
            f = QFont()
            f.setPointSize(16)
            f.setBold(True)
            p.setFont(f)
            p.drawText(14, 30, "● REC")
        p.end()
        self.view.setPixmap(pix)

    # ---------------------------------------------------- daemon / warmup
    def on_warmup_clicked(self):
        if self.daemon_up:
            if self.state == "recording":
                QMessageBox.warning(self, "녹화 중", "녹화 중에는 데몬을 끌 수 없습니다.")
                return
            self.btn_warm.setEnabled(False)
            self._spawn(self.daemon.shutdown, done=lambda _r: self._after_warm(False))
        else:
            self.btn_warm.setEnabled(False)
            self.log("워밍업(데몬 배포·시작) 진행 중...")
            d = self.cfg.daemon
            self._spawn(self.daemon.ensure_running, d.stream_cams, d.stream_fps,
                        d.idle_timeout_s, log=lambda m: None,
                        done=lambda st: self._after_warm(True, st),
                        error=lambda m: (self.btn_warm.setEnabled(True),
                                         self.log("워밍업 실패: " + m.splitlines()[0])))

    def _after_warm(self, up, st=None):
        self.btn_warm.setEnabled(True)
        self._apply_daemon_status(st if up else None)
        self.log("워밍업 완료 — 녹화 즉시 시작 가능" if up else "데몬 종료됨")

    def _poll_daemon(self):
        self._spawn(self.daemon.status, done=self._apply_daemon_status)

    def _apply_daemon_status(self, st):
        up = bool(st and st.get("ok"))
        self.daemon_up = up
        self.stream.enabled = up
        extra = ""
        if up:
            extra = f" (업타임 {int(st['uptime_s'] // 60)}분, 토픽 {st.get('n_topics')}개)"
            if st.get("recording") and self.state == "idle":
                extra += f" ⚠️ 데몬이 녹화 중: {st['recording'][:8]}"
        self.lbl_daemon.setText(f"{DOT['ok' if up else 'unknown']} 데몬{extra}")
        self.btn_warm.setText("🧊 워밍업 종료" if up else "🔥 워밍업 시작")
        if not up:
            self.view.setText("워밍업을 시작하면\n카메라 스트림이 표시됩니다")

    def _heartbeat(self):
        if self.daemon_up:
            self._spawn(self.daemon.heartbeat)

    # ---------------------------------------------------------- connection
    def check_connections(self):
        if self.cfg.mode == "local":
            self.lbl_server.setText(f"{DOT['unknown']} 서버 (로컬 모드 — 미사용)")
        else:
            self._spawn(self.api.ping, done=lambda ok: self.lbl_server.setText(
                f"{DOT['ok' if ok else 'bad']} 서버"))

        def chk_robot():
            from bridge.ssh_link import SSHSession
            try:
                s = SSHSession(self.cfg.robot.ssh_host, self.cfg.robot.ssh_user,
                               self.cfg.robot.ssh_password, timeout=6)
                s.close()
                return True
            except Exception:
                return False

        self._spawn(chk_robot, done=lambda ok: self.lbl_robot.setText(
            f"{DOT['ok' if ok else 'bad']} 로봇"))

    # ---------------------------------------------------------------- mode
    def _apply_mode(self, initial=False):
        mode = self.cmb_mode.currentData()
        self.cfg.mode = mode
        local = mode == "local"
        for w in (self.lbl_project, self.cmb_project, self.lbl_job, self.cmb_job):
            w.setVisible(not local)
        for w in (self.lbl_dataset, self.cmb_dataset, self.ds_dir_row,
                  self.btn_ds_new, self.btn_ds_eps):
            w.setVisible(local)
        self.lbl_desc.setText("Task" if local else "설명")
        self.edt_desc.setPlaceholderText(
            "에피소드 task instruction (예: pick up the bottle)" if local
            else "에피소드 설명 (예: pick up the bottle)")
        if not initial:
            cfgmod.save_user_prefs(self.cfg)
            self.check_connections()
            self.refresh_targets()
            self.log(f"저장 모드: {'로컬 (LeRobot append)' if local else '서버 업로드'}")

    def refresh_targets(self):
        if self.cfg.mode == "local":
            self.refresh_local_datasets()
        else:
            self.refresh_projects()

    def refresh_local_datasets(self):
        names = local_store.list_datasets(self.cfg)
        cur = self.cfg.local.dataset or (names[0] if names else "")
        self.cmb_dataset.blockSignals(True)
        self.cmb_dataset.clear()
        self.cmb_dataset.addItems(names)
        self.cmb_dataset.setCurrentText(cur)
        self.cmb_dataset.blockSignals(False)
        self._update_dataset_info()

    def _update_dataset_info(self):
        from pathlib import Path
        self.lbl_ds_info.setText(
            f"데이터 저장위치: {Path(self.cfg.local.data_dir).expanduser()}")

    def on_change_data_dir(self):
        from pathlib import Path
        cur = str(Path(self.cfg.local.data_dir).expanduser())
        path = QFileDialog.getExistingDirectory(self, "데이터 저장위치 선택", cur)
        if not path:
            return
        self.cfg.local.data_dir = path
        cfgmod.save_user_prefs(self.cfg)
        self.refresh_local_datasets()      # 새 위치의 데이터셋 목록으로 갱신
        self.log(f"데이터 저장위치 변경: {path}")

    def on_new_dataset(self):
        name, ok = QInputDialog.getText(
            self, "새 데이터셋", "데이터셋 이름 (첫 녹화 시 폴더가 생성됩니다):")
        name = (name or "").strip()
        if not ok or not name:
            return
        if "/" in name or name.startswith("."):
            QMessageBox.warning(self, "이름 오류", "폴더명으로 쓸 수 없는 이름입니다.")
            return
        if self.cmb_dataset.findText(name) < 0:
            self.cmb_dataset.addItem(name)
        self.cmb_dataset.setCurrentText(name)
        self.cfg.local.dataset = name
        cfgmod.save_user_prefs(self.cfg)
        self.log(f"새 데이터셋 지정: {name} (첫 녹화 때 생성)")

    def on_show_episodes(self):
        name = self.cmb_dataset.currentText().strip()
        if not name:
            QMessageBox.information(self, "데이터셋 없음", "데이터셋 이름을 먼저 지정하세요.")
            return
        eps = local_store.list_episodes(self.cfg, name)
        dlg = QDialog(self)
        dlg.setWindowTitle(f"에피소드 목록 — {name} ({len(eps)}개)")
        dlg.resize(860, 420)
        v = QVBoxLayout(dlg)
        tbl = QTableWidget(len(eps), 4)
        tbl.setHorizontalHeaderLabels(["#", "프레임", "Task", "Raw 원본 위치 (더블클릭 = 열기)"])
        tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for r, e in enumerate(eps):
            for c, val in enumerate([str(e["index"]), str(e["length"]),
                                     e["task"], e["raw"] or "(raw 미보관)"]):
                tbl.setItem(r, c, QTableWidgetItem(val))
        tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        tbl.horizontalHeader().setStretchLastSection(True)
        tbl.cellDoubleClicked.connect(lambda r, _c: QDesktopServices.openUrl(
            QUrl.fromLocalFile(eps[r]["raw"])) if eps[r]["raw"] else None)
        v.addWidget(tbl)
        h = QHBoxLayout()
        btn_open = QPushButton("데이터셋 폴더 열기")
        btn_open.clicked.connect(lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(local_store.dataset_root(self.cfg, name)))))
        h.addWidget(btn_open)
        h.addStretch(1)
        btn_close = QPushButton("닫기")
        btn_close.clicked.connect(dlg.accept)
        h.addWidget(btn_close)
        v.addLayout(h)
        dlg.exec()

    # ------------------------------------------------------------ projects
    def refresh_projects(self):
        def done(projects):
            self.projects = projects
            self.cmb_project.blockSignals(True)
            self.cmb_project.clear()
            for p in projects:
                self.cmb_project.addItem(f"{p['name']} ({p.get('robot_model', '?')})", p["id"])
            self.cmb_project.blockSignals(False)
            self.on_project_changed()

        self._spawn(self.api.projects, done=done,
                    error=lambda m: self.log("프로젝트 조회 실패: " + m.splitlines()[0]))

    def on_project_changed(self):
        pid = self.cmb_project.currentData()
        if pid is None:
            self.cmb_job.clear()
            return
        self._spawn(self.api.jobs, pid, done=self._fill_jobs)

    def _fill_jobs(self, jobs):
        self.cmb_job.clear()
        for j in jobs:
            self.cmb_job.addItem(j["name"], j["id"])

    # ------------------------------------------------------------ recording
    def on_record_clicked(self):
        if self.state == "idle":
            self.begin_countdown()
        elif self.state == "counting":
            self.cd_timer.stop()
            self._set_state("idle")
            self._paint_view()
            self.log("카운트다운 취소")
        elif self.state == "recording":
            self.stop_recording()

    def begin_countdown(self):
        if not self.daemon_up:
            QMessageBox.warning(self, "워밍업 필요", "먼저 워밍업을 시작하세요.")
            return
        if self.cfg.mode == "local":
            name = self.cmb_dataset.currentText().strip()
            if not name:
                QMessageBox.warning(self, "데이터셋 없음", "누적할 데이터셋 이름을 입력하세요.")
                return
            self.cfg.local.dataset = name
            cfgmod.save_user_prefs(self.cfg)
        elif self.cmb_job.currentData() is None:
            QMessageBox.warning(self, "Job 없음", "녹화할 Job을 먼저 선택하세요.")
            return
        n = self.spn_cd.value()
        if n <= 0:
            self.start_recording()
            return
        self.countdown_left = n
        self._set_state("counting")
        self._paint_view()
        self.cd_timer.start()

    def _countdown_tick(self):
        self.countdown_left -= 1
        if self.countdown_left <= 0:
            self.cd_timer.stop()
            self.start_recording()
        else:
            self._paint_view()

    def start_recording(self):
        self.rec_uuid = str(uuidlib.uuid4())
        self._set_state("starting")

        def done(res):
            if res.get("ok"):
                self.rec_t0 = time.time()
                self._set_state("recording")
                self.log(f"녹화 시작 ({res.get('n_topics')}토픽, {len(res.get('cameras', []))}캠) {self.rec_uuid[:8]}")
            else:
                self._set_state("idle")
                self.log(f"녹화 시작 실패: {res}")

        self._spawn(self.daemon.start, self.rec_uuid, self.cfg.recording.post_win_ms,
                    self.cfg.recording.cameras, done=done,
                    error=lambda m: (self._set_state("idle"), self.log("시작 오류: " + m.splitlines()[0])))

    def stop_recording(self):
        self._set_state("stopping")
        duration = int(time.time() - self.rec_t0)

        def done(_res):
            if self.cfg.mode == "local":
                dataset = self.cmb_dataset.currentText().strip()
                self.log(f"녹화 종료 ({duration}s) — 로컬 저장 큐에 추가 → {dataset}")
                self.uploads.enqueue(
                    uuid=self.rec_uuid, mode="local",
                    project_id=0, project_name="(로컬)",
                    job_id=0, job_name=dataset,
                    description=self.edt_desc.text().strip() or dataset,
                    duration_s=duration)
            else:
                self.log(f"녹화 종료 ({duration}s) — 백그라운드 업로드 큐에 추가")
                self.uploads.enqueue(
                    uuid=self.rec_uuid,
                    project_id=self.cmb_project.currentData(),
                    project_name=self.cmb_project.currentText(),
                    job_id=self.cmb_job.currentData(),
                    job_name=self.cmb_job.currentText(),
                    description=self.edt_desc.text().strip() or self.cmb_job.currentText(),
                    duration_s=duration)
            self._set_state("idle")
            self.lbl_elapsed.setText("00:00")
            self._paint_view()

        self._spawn(self.daemon.stop, self.rec_uuid, self.cfg.recording.post_win_ms,
                    done=done,
                    error=lambda m: (self._set_state("idle"), self.log("종료 오류: " + m.splitlines()[0])))

    # -------------------------------------------------------------- upload
    def refresh_upload_table(self):
        jobs = list(self.uploads.jobs)
        self.tbl.setRowCount(len(jobs))
        for r, j in enumerate(reversed(jobs)):
            names = pipeline.step_names(getattr(j, "mode", "server"))
            step_txt = ""
            if j.status == "running" and j.current_step >= 0:
                step_txt = f"{STEP_ICON['run']} {names[j.current_step]} ({j.current_step + 1}/{len(names)})"
            elif j.status == "done" and getattr(j, "mode", "server") == "local":
                step_txt = f"✅ 완료 ({j.n_frames}프레임 → {j.job_name})"
            elif j.status == "done":
                step_txt = f"✅ 완료 ({j.n_frames}프레임, dataset {j.dataset_id})"
            elif j.status == "error":
                step_txt = f"❌ {j.error[:60]}"
            elif j.status == "queued":
                step_txt = "대기"
            status_icon = {"queued": "⏸ 대기", "running": "▶ 진행", "done": "✅ 완료",
                           "error": "❌ 실패"}[j.status]
            cells = [j.created_at, j.uuid[:8], f"{j.project_name} / {j.job_name}",
                     f"{j.duration_s}s", step_txt, status_icon]
            for c, text in enumerate(cells):
                self.tbl.setItem(r, c, QTableWidgetItem(text))
        n_pending = self.uploads.pending_count()
        self.lbl_upload_summary.setText(f"업로드 대기·진행 {n_pending}건")
        idx = self.sidebar.item(1)
        idx.setText(f"⇪  업로드 ({n_pending})" if n_pending else "⇪  업로드")

    def on_retry_failed(self):
        for j in self.uploads.jobs:
            if j.status == "error":
                self.uploads.retry(j.uuid)

    # ---------------------------------------------------------------- exit
    def closeEvent(self, ev):
        if self.state == "recording":
            r = QMessageBox.question(self, "녹화 중", "녹화 중입니다. 녹화를 종료하고 나갈까요?")
            if r != QMessageBox.StandardButton.Yes:
                ev.ignore()
                return
            try:
                self.daemon.stop(self.rec_uuid, self.cfg.recording.post_win_ms)
            except Exception:
                pass
        n = self.uploads.pending_count()
        if n:
            QMessageBox.information(
                self, "업로드 미완료",
                f"업로드 {n}건이 남아 있습니다. 다음 실행 시 자동으로 이어서 처리됩니다.")
        if self.daemon_up:
            self.daemon.shutdown()   # 워치독도 있지만 정상 종료 시 바로 정리
        self.stream.stop()
        self.stream.wait(2000)
        ev.accept()


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
