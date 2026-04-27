#!/usr/bin/env python3
# iceq_torno.py — IHM PyQt5 para TORNO ICEQ
# Destino: configs/torno_iceq/iceq_torno.py
#
# Correções aplicadas (migração rasph → torno_iceq):
#   [FIX-1] Joint map corrigido: X=joint0, Z=joint1 (trivkins XZC estável)
#   [FIX-2] pos[1] para Z no cabeçalho (não pos[2] que é eixo C)
#   [FIX-3] Caminho cloud via INI_FILE_NAME dinâmico
#   [FIX-4] lbl_rpm_tittle → lbl_rpm_title (nome real no .ui)
#   [FIX-5] _spindle_rpm_bar usa lbl_rpm_title (corrigido)
#   [FIX-6] ref_y conectado (HAS_Y_AXIS=False mantido)

import sys
import os
from PyQt5 import QtWidgets, uic, QtCore, QtGui
from PyQt5.QtWidgets import QFileDialog
import linuxcnc
import time
import hal
import threading
import re
import math

# ─────────────────────────────────────────────────────────────
# Cloud client — importa da pasta iceq_cloud (versão robusta)
# ─────────────────────────────────────────────────────────────
try:
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    _cloud_dir  = os.path.join(_script_dir, "iceq_cloud")
    if _cloud_dir not in sys.path:
        sys.path.insert(0, _cloud_dir)
    from iceq_cloud_client import IceqCloudClient as _IceqCloudClient
except Exception as _ce:
    print(f"[ICEQ][CLOUD] import cloud falhou: {_ce}")
    _IceqCloudClient = None


def _get_cloud_config_path() -> str:
    """
    Localiza o iceq_cloud_config.json relativo ao INI atual.
    Prioridade:
      1) Mesma pasta do .ini (INI_FILE_NAME)
      2) Pasta do script Python
    """
    for env in ("INI_FILE_NAME", "EMC2_INI_FILE_NAME", "LINUXCNC_INI"):
        ini = os.environ.get(env, "")
        if ini:
            candidate = os.path.join(os.path.dirname(ini), "iceq_cloud", "iceq_cloud_config.json")
            if os.path.isfile(candidate):
                return candidate

    # fallback: pasta do script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(script_dir, "iceq_cloud", "iceq_cloud_config.json")


# ─────────────────────────────────────────────────────────────
# Preview 2D
# ─────────────────────────────────────────────────────────────
class IceqPreview2D(QtCore.QObject):
    """
    Preview 2D simples em XZ para LinuxCNC:
    Interpreta G0/G1/G2/G3 (plano XZ) e desenha caminhos.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self._view = None
        self._scene = None
        self._container = None

        self._last_file = ""
        self._last_mtime = 0.0

        self._segments = []
        self._items = []
        self._bbox = None

        self._active_src_line = -1
        self._active_seg_idx = set()

        self._pos_item = None
        self._pos_radius = 2.5

        self._zoom_factor_step = 1.20
        self._zoom_min = 0.10
        self._zoom_max = 20.0
        self._default_transform = None

        self.finish_feed_threshold = 120.0
        self.arc_steps_per_rev = 180

        self._pen_cut = QtGui.QPen(QtGui.QColor(60, 170, 255))
        self._pen_cut.setWidthF(1.6)

        self._pen_finish = QtGui.QPen(QtGui.QColor(255, 210, 80))
        self._pen_finish.setWidthF(3.2)

        self._pen_rapid = QtGui.QPen(QtGui.QColor(170, 170, 170))
        self._pen_rapid.setWidthF(1.4)
        self._pen_rapid.setStyle(QtCore.Qt.DashLine)

        self._pen_axis = QtGui.QPen(QtGui.QColor(90, 90, 90))
        self._pen_axis.setWidthF(1.0)

        self._pen_active_cut = QtGui.QPen(QtGui.QColor(255, 80, 80))
        self._pen_active_cut.setWidthF(3.0)

        self._pen_active_finish = QtGui.QPen(QtGui.QColor(255, 80, 80))
        self._pen_active_finish.setWidthF(4.2)

        self._pen_active_rapid = QtGui.QPen(QtGui.QColor(255, 80, 80))
        self._pen_active_rapid.setWidthF(2.6)
        self._pen_active_rapid.setStyle(QtCore.Qt.DashLine)

        self._brush_pos = QtGui.QBrush(QtGui.QColor(255, 70, 70))

    def attach(self, container_widget: QtWidgets.QWidget):
        self._container = container_widget
        self._view = QtWidgets.QGraphicsView(self._container)
        self._scene = QtWidgets.QGraphicsScene(self._view)
        self._view.setScene(self._scene)

        self._view.setRenderHint(QtGui.QPainter.Antialiasing, True)
        self._view.setViewportUpdateMode(QtWidgets.QGraphicsView.FullViewportUpdate)
        self._view.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self._view.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)

        lay = self._container.layout()
        if lay is None:
            lay = QtWidgets.QVBoxLayout(self._container)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(0)
            self._container.setLayout(lay)

        lay.addWidget(self._view)
        self._default_transform = QtGui.QTransform(self._view.transform())
        self.clear()

    def clear(self):
        if self._scene is None:
            return
        self._scene.clear()
        self._segments = []
        self._items = []
        self._bbox = None
        self._pos_item = None
        self._active_src_line = -1
        self._active_seg_idx = set()
        self._draw_axes()

    def _draw_axes(self):
        if self._scene is None:
            return
        self._scene.addLine(-200, 0, 200, 0, self._pen_axis)
        self._scene.addLine(0, -200, 0, 200, self._pen_axis)

    def tick_live(self, stat):
        if self._scene is None:
            return
        try:
            if hasattr(stat, "actual_position"):
                x = float(stat.actual_position[0])
                z = float(stat.actual_position[1])   # [FIX-2] Z = index 1
            else:
                x = float(stat.position[0])
                z = float(stat.position[1])           # [FIX-2]
        except Exception:
            return

        px = z
        py = -x

        if self._pos_item is None:
            self._pos_item = self._scene.addEllipse(
                px - self._pos_radius, py - self._pos_radius,
                2 * self._pos_radius, 2 * self._pos_radius,
                QtGui.QPen(QtCore.Qt.NoPen),
                self._brush_pos
            )
        else:
            self._pos_item.setRect(
                px - self._pos_radius, py - self._pos_radius,
                2 * self._pos_radius, 2 * self._pos_radius
            )

    def ensure_program_loaded(self, filepath: str):
        if not filepath:
            return
        try:
            fi = QtCore.QFileInfo(filepath)
            if not fi.exists():
                return
            mtime = fi.lastModified().toSecsSinceEpoch()
        except Exception:
            mtime = 0.0

        if filepath == self._last_file and mtime == self._last_mtime:
            return

        self._last_file = filepath
        self._last_mtime = mtime
        self.load_program(filepath)

    def load_program(self, filepath: str):
        if self._scene is None:
            return
        self.clear()
        try:
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except Exception:
            return

        segs, bbox = self._parse_gcode_to_segments(lines)
        self._segments = segs
        self._bbox = bbox
        self._draw_segments()
        self._fit_view()

    def _fit_view(self):
        if self._view is None or self._scene is None:
            return
        if not self._bbox:
            self._view.fitInView(self._scene.itemsBoundingRect(), QtCore.Qt.KeepAspectRatio)
            return
        (minz, minx, maxz, maxx) = self._bbox
        rect = QtCore.QRectF(minz, -maxx, (maxz - minz), (maxx - minx))
        rect = rect.adjusted(-10, -10, 10, 10)
        self._view.fitInView(rect, QtCore.Qt.KeepAspectRatio)

    def _draw_segments(self):
        if self._scene is None:
            return
        self._items = []
        for s in self._segments:
            x1, z1, x2, z2 = s["x1"], s["z1"], s["x2"], s["z2"]
            stype = s["type"]
            finish = s["finish"]
            p1x, p1y = z1, -x1
            p2x, p2y = z2, -x2
            if stype == "rapid":
                pen = self._pen_rapid
            else:
                pen = self._pen_finish if finish else self._pen_cut
            it = self._scene.addLine(p1x, p1y, p2x, p2y, pen)
            self._items.append(it)

    def zoom_in(self):
        self._apply_zoom(self._zoom_factor_step)

    def zoom_out(self):
        self._apply_zoom(1.0 / self._zoom_factor_step)

    def fit_all(self):
        self._fit_view()

    def reset_view(self):
        if self._view is None:
            return
        if self._default_transform is not None:
            self._view.setTransform(self._default_transform)
        else:
            self._view.resetTransform()
        self._view.centerOn(0.0, 0.0)

    def _apply_zoom(self, factor: float):
        if self._view is None:
            return
        try:
            cur = float(self._view.transform().m11())
        except Exception:
            cur = 1.0
        new_scale = cur * float(factor)
        if new_scale < self._zoom_min:
            factor = self._zoom_min / max(cur, 1e-9)
        elif new_scale > self._zoom_max:
            factor = self._zoom_max / max(cur, 1e-9)
        self._view.setTransformationAnchor(QtWidgets.QGraphicsView.AnchorViewCenter)
        self._view.scale(float(factor), float(factor))

    def tick_program_line(self, stat):
        if self._scene is None or not self._segments or not self._items:
            return
        line_no = -1
        try:
            line_no = int(getattr(stat, "motion_line", -1))
        except Exception:
            line_no = -1
        if line_no < 0:
            try:
                line_no = int(getattr(stat, "current_line", -1))
            except Exception:
                line_no = -1
        if line_no < 0:
            return
        if line_no == self._active_src_line:
            return
        self.set_active_src_line(line_no)

    def set_active_src_line(self, src_line: int):
        if self._scene is None:
            return
        if self._active_seg_idx:
            for i in self._active_seg_idx:
                if 0 <= i < len(self._segments) and 0 <= i < len(self._items):
                    s = self._segments[i]
                    it = self._items[i]
                    it.setPen(self._pen_for_segment(s, active=False))
        new_idx = set()
        for i, s in enumerate(self._segments):
            if int(s.get("src_line", -1)) == int(src_line):
                if i < len(self._items):
                    self._items[i].setPen(self._pen_for_segment(s, active=True))
                    new_idx.add(i)
        self._active_src_line = int(src_line)
        self._active_seg_idx = new_idx

    def _pen_for_segment(self, s: dict, active: bool = False) -> QtGui.QPen:
        stype = s.get("type", "cut")
        finish = bool(s.get("finish", False))
        if not active:
            if stype == "rapid":
                return self._pen_rapid
            return (self._pen_finish if finish else self._pen_cut)
        if stype == "rapid":
            return self._pen_active_rapid
        return (self._pen_active_finish if finish else self._pen_active_cut)

    def _parse_gcode_to_segments(self, lines):
        x = 0.0
        z = 0.0
        feed = None
        motion = 0
        abs_mode = True
        segs = []
        minx = maxx = x
        minz = maxz = z

        def update_bbox(px, pz):
            nonlocal minx, maxx, minz, maxz
            minx = min(minx, px)
            maxx = max(maxx, px)
            minz = min(minz, pz)
            maxz = max(maxz, pz)

        def is_finish_line(raw_line: str, local_feed):
            u = raw_line.upper()
            if "(FINISH)" in u or ";FINISH" in u or " FINISH" in u:
                return True
            try:
                if local_feed is not None and float(local_feed) > 0.0:
                    return float(local_feed) <= float(self.finish_feed_threshold)
            except Exception:
                pass
            return False

        def strip_comments_keep(raw: str):
            raw2 = re.sub(r"\(.*?\)", " ", raw)
            raw2 = raw2.split(";")[0]
            return raw2

        word_re = re.compile(r"([A-Z])\s*([+\-]?\d+(\.\d+)?)", re.I)

        for idx0, raw in enumerate(lines):
            src_line = idx0 + 1
            raw_line = raw.rstrip("\n")
            line = strip_comments_keep(raw_line).strip().upper()
            if not line:
                continue

            words = dict()
            for m in word_re.finditer(line):
                words[m.group(1).upper()] = float(m.group(2))

            if "G" in words:
                gval = int(round(words["G"]))
                if gval == 90:
                    abs_mode = True
                elif gval == 91:
                    abs_mode = False
                elif gval in (0, 1, 2, 3):
                    motion = gval

            if "F" in words:
                feed = float(words["F"])

            tx = x
            tz = z
            if "X" in words:
                tx = (words["X"] if abs_mode else (x + words["X"]))
            if "Z" in words:
                tz = (words["Z"] if abs_mode else (z + words["Z"]))

            if tx == x and tz == z and ("I" not in words and "K" not in words):
                continue

            finish = (motion == 1) and is_finish_line(raw_line, feed)

            if motion in (0, 1):
                segs.append({
                    "x1": x, "z1": z,
                    "x2": tx, "z2": tz,
                    "type": ("rapid" if motion == 0 else "cut"),
                    "finish": bool(finish),
                    "src_line": int(src_line),
                })
                x, z = tx, tz
                update_bbox(x, z)

            elif motion in (2, 3):
                if "I" not in words and "K" not in words:
                    segs.append({
                        "x1": x, "z1": z,
                        "x2": tx, "z2": tz,
                        "type": "cut",
                        "finish": bool(finish),
                        "src_line": int(src_line),
                    })
                    x, z = tx, tz
                    update_bbox(x, z)
                    continue

                cx = x + float(words.get("I", 0.0))
                cz = z + float(words.get("K", 0.0))
                sx, sz = x, z
                ex, ez = tx, tz
                a0 = math.atan2(sz - cz, sx - cx)
                a1 = math.atan2(ez - cz, ex - cx)

                if motion == 2:
                    if a1 >= a0:
                        a1 -= 2.0 * math.pi
                    da = a1 - a0
                else:
                    if a1 <= a0:
                        a1 += 2.0 * math.pi
                    da = a1 - a0

                r = math.hypot(sx - cx, sz - cz)
                steps = max(12, int(abs(da) / (2.0 * math.pi) * self.arc_steps_per_rev))
                prevx, prevz = sx, sz

                for i in range(1, steps + 1):
                    t = i / float(steps)
                    ang = a0 + da * t
                    nx = cx + r * math.cos(ang)
                    nz = cz + r * math.sin(ang)
                    segs.append({
                        "x1": prevx, "z1": prevz,
                        "x2": nx, "z2": nz,
                        "type": "cut",
                        "finish": bool(finish),
                        "src_line": int(src_line),
                    })
                    prevx, prevz = nx, nz
                    update_bbox(nx, nz)

                x, z = ex, ez
                update_bbox(x, z)

        bbox = (minz, minx, maxz, maxx)
        return segs, bbox


# ─────────────────────────────────────────────────────────────
# Configuração da máquina
# ─────────────────────────────────────────────────────────────
HAS_Y_AXIS = False   # Torno: apenas X e Z


# ─────────────────────────────────────────────────────────────
# Janela principal
# ─────────────────────────────────────────────────────────────
class IceqMainWindow(QtWidgets.QMainWindow):

    # ── [FIX-1] Mapeamento correto trivkins XZC: X=joint0, Z=joint1 ──
    # Padrão torno_iceq estável (stepgen.00=X, stepgen.01=Z, stepgen.02=C)
    _JOINT_MAP = {"X": 0, "Z": 1}   # C (torre) = 2, não usado no JOG

    def __init__(self):
        super().__init__()

        # Estilo Qt
        try:
            app = QtWidgets.QApplication.instance()
            if app is not None:
                wanted = "Cleanlooks"
                if wanted in QtWidgets.QStyleFactory.keys():
                    app.setStyle(wanted)
                else:
                    app.setStyle("Fusion")
        except Exception:
            pass

        # Carrega .ui da mesma pasta do script
        _ui_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "iceq_torno.ui")
        uic.loadUi(_ui_path, self)

        # Preview 2D
        self.preview2d = IceqPreview2D(self)
        self.preview2d.attach(self.w_preview_2d)
        self._preview2d_last_stat_file = ""

        # Barra RPM spindle — [FIX-4/5] usa lbl_rpm_title (sem duplo t)
        try:
            self._init_spindle_rpm_bar()
        except Exception as e:
            print(f"[ICEQ] init spindle rpm bar erro: {e}")

        try:
            f = self.lbl_rpm_max.font()
            f.setPointSize(10)
            self.lbl_rpm_max.setFont(f)
        except Exception:
            pass

        # Editor
        self._editor_current_file = None
        self.btn_editor_save.clicked.connect(self._editor_save)

        # Programa
        self._program_loaded_path = None

        # Preview — botões
        try:
            self.btn_preview_zoom_plus.clicked.connect(self.preview2d.zoom_in)
            self.btn_preview_zoom_minus.clicked.connect(self.preview2d.zoom_out)
            self.btn_preview_auto.clicked.connect(self.preview2d.fit_all)
            self.btn_preview_reset.clicked.connect(self.preview2d.reset_view)
        except Exception:
            pass

        # HAL component para JOG contínuo via HALUI
        self._hal_jog_comp = None
        self._hal_jog_ready = False
        self._hal_jog_pins = {}
        self._init_hal_jog_component()

        # LinuxCNC
        self.cmd = linuxcnc.command()
        self._dbg_last_status = None
        self.stat = linuxcnc.stat()
        self._cmd_lock = threading.RLock()
        self._mdi_busy = False
        self._mdi_fsm_timer = None
        self._mdi_fsm_state = ""
        self._mdi_fsm_sent = False
        self._mdi_sent_ts = 0.0
        self._mdi_deadline = 0.0
        self._mdi_last_cmd = ""

        self._spindle_rpm_setpoint = 0.0

        # Progresso do programa
        self._gcode_total_lines = 0
        self._last_progress_pct = 0

        if hasattr(self, "prg_cycle_top"):
            self.prg_cycle_top.setRange(0, 100)
            self.prg_cycle_top.setValue(0)
            self.prg_cycle_top.setTextVisible(True)
            self.prg_cycle_top.setFormat("%p%")

        # Ciclo: tempo
        self._cycle_running = False
        self._cycle_start_ts = None
        self._cycle_last_elapsed = 0.0
        self._gcode_loaded_path = None

        # Índices de eixo
        self.AXIS_X = 0
        self.AXIS_Z = 1   # [FIX-2] Z = joint 1

        # Botões do cabeçalho
        self.btn_machine_off_top.clicked.connect(self.toggle_machine)
        self.btn_emergencia_bottom.clicked.connect(self.toggle_estop)
        self.btn_start_cycle.clicked.connect(self.cycle_start_toggle)
        self.btn_stop_cycle.clicked.connect(self.cycle_stop)

        # Timer de status
        self.status_timer = QtCore.QTimer(self)
        self.status_timer.timeout.connect(self.update_status_panel)
        self.status_timer.start(200)

        # ── [FIX-3] Cloud: caminho dinâmico via INI_FILE_NAME ──
        self.cloud = None
        if _IceqCloudClient is not None:
            try:
                _cfg_path = _get_cloud_config_path()
                self.cloud = _IceqCloudClient(_cfg_path)
                print(f"[ICEQ][CLOUD] config: {_cfg_path}")

                def _cloud_boot():
                    try:
                        if self.cloud and self.cloud.is_configured():
                            self.cloud.send_startup_log("IHM ICEQ iniciou")
                            self.cloud.send_ping()
                        else:
                            print("[ICEQ][CLOUD] Não configurado.")
                    except Exception as e:
                        print(f"[ICEQ][CLOUD] boot erro: {e}")

                threading.Thread(target=_cloud_boot, daemon=True).start()
            except Exception as e:
                print(f"[ICEQ][CLOUD] init falhou: {e}")

        # Cloud timers
        self._cloud_enabled = False
        self._cloud_last_state = None
        self._cloud_last_sent_ts = {}
        self._cloud_min_interval_s = 1.0

        try:
            self._cloud_enabled = bool(self.cloud and self.cloud.is_configured())
        except Exception:
            self._cloud_enabled = False

        self._cloud_ping_timer = QtCore.QTimer(self)
        self._cloud_ping_timer.timeout.connect(self._cloud_ping_tick)

        self._cloud_transition_timer = QtCore.QTimer(self)
        self._cloud_transition_timer.timeout.connect(self._cloud_transition_tick)

        if self._cloud_enabled:
            self._cloud_ping_timer.start(20000)
            self._cloud_transition_timer.start(500)

        # Pisca / intertravamento visual
        self._blink_phase = False
        self._blink_timer = QtCore.QTimer(self)
        self._blink_timer.timeout.connect(self._blink_tick)
        self._blink_timer.start(400)

        self._btn_style_default = {}
        self._btn_size_default = {}

        for _btn_name in ("btn_emergencia_bottom", "btn_machine_off_top", "btn_ref_all", "btn_start_cycle"):
            if hasattr(self, _btn_name):
                btn = getattr(self, _btn_name)
                try:
                    self._btn_style_default[_btn_name] = btn.styleSheet() or ""
                except Exception:
                    self._btn_style_default[_btn_name] = ""
                try:
                    self._btn_size_default[_btn_name] = (
                        int(btn.minimumWidth()), int(btn.minimumHeight()),
                        int(btn.maximumWidth()), int(btn.maximumHeight()),
                    )
                except Exception:
                    self._btn_size_default[_btn_name] = None

        # AT SPEED (debounce)
        self._sp_at_speed_tol_abs_rpm = 50.0
        self._sp_at_speed_tol_pct = 0.05
        self._sp_at_speed_required_hits = 3
        self._sp_at_speed_cnt = 0
        self._sp_at_speed_last = False

        self.update_status_panel()

        # Abrir programa
        self.btn_open_program_edit.clicked.connect(self.open_program)
        self.btn_open_program_main.clicked.connect(self.open_program)

        # Ferramentas T1..T16
        self._init_tool_buttons()
        self._set_tool_buttons_enabled(False)

        # MDI
        if hasattr(self, "btn_mdi_send"):
            self.btn_mdi_send.clicked.connect(self.on_mdi_send)
        if hasattr(self, "txt_mdi_entry"):
            try:
                self.txt_mdi_entry.returnPressed.connect(self.on_mdi_send)
            except Exception:
                pass
        if hasattr(self, "txt_mdi_history"):
            try:
                self.txt_mdi_history.setReadOnly(True)
            except Exception:
                pass
            try:
                self.txt_mdi_history.installEventFilter(self)
                self.txt_mdi_history.viewport().installEventFilter(self)
            except Exception:
                pass
            try:
                self._mdi_hist_max_lines = 400
                self._mdi_history_loaded = False
                self._mdi_history_load()
            except Exception as e:
                print(f"[ICEQ] MDI history load erro: {e}")
            try:
                font = self.txt_mdi_history.font()
                font.setPointSize(15)
                self.txt_mdi_history.setFont(font)
            except Exception:
                pass

        # Spindle estado interno
        self._spindle_rpm_setpoint = 0
        self._spindle_dir = 0
        self._spindle_running = False
        self._coolant_on = False
        self._spindle_step = 100

        if hasattr(self, "btn_spindle_rpm_plus"):
            self.btn_spindle_rpm_plus.clicked.connect(self.spindle_rpm_plus)
        if hasattr(self, "btn_spindle_rpm_minus"):
            self.btn_spindle_rpm_minus.clicked.connect(self.spindle_rpm_minus)
        if hasattr(self, "btn_spindle_cw"):
            self.btn_spindle_cw.clicked.connect(self.spindle_cw)
        if hasattr(self, "btn_spindle_ccw"):
            self.btn_spindle_ccw.clicked.connect(self.spindle_ccw)
        if hasattr(self, "btn_spindle_stop"):
            self.btn_spindle_stop.clicked.connect(self.spindle_stop)
        if hasattr(self, "btn_refri_button"):
            self.btn_refri_button.clicked.connect(self.coolant_toggle)

        # Referência / home
        self.btn_ref_all.clicked.connect(self.ref_all)
        self.btn_ref_x.clicked.connect(self.ref_x)

        if HAS_Y_AXIS:
            self.btn_ref_y.clicked.connect(self.ref_y)
            self.btn_ref_y.setEnabled(True)
        else:
            self.btn_ref_y.setEnabled(False)

        self.btn_ref_z.clicked.connect(self.ref_z)
        self.btn_zero_peca_g54.clicked.connect(self.zero_g54)

        # Overrides
        self._machine_ovr_pct = 100
        self._spindle_ovr_pct = 100

        if hasattr(self, "sld_vel_machine_oper"):
            self.sld_vel_machine_oper.setRange(0, 120)
            self.sld_vel_machine_oper.setValue(100)
        if hasattr(self, "spn_vel_machine_oper"):
            self.spn_vel_machine_oper.setRange(0, 120)
            self.spn_vel_machine_oper.setValue(100)
            try:
                self.spn_vel_machine_oper.setSuffix("%")
            except Exception:
                pass
        if hasattr(self, "sld_vel_spindle_oper"):
            self.sld_vel_spindle_oper.setRange(0, 120)
            self.sld_vel_spindle_oper.setValue(100)
        if hasattr(self, "spn_vel_spindle_oper"):
            self.spn_vel_spindle_oper.setRange(0, 120)
            self.spn_vel_spindle_oper.setValue(100)
            try:
                self.spn_vel_spindle_oper.setSuffix("%")
            except Exception:
                pass

        if hasattr(self, "sld_vel_machine_oper"):
            try:
                self.sld_vel_machine_oper.valueChanged.disconnect()
            except Exception:
                pass
            self.sld_vel_machine_oper.valueChanged.connect(self._dbg_machine_ovr_changed)

        if hasattr(self, "spn_vel_machine_oper"):
            try:
                self.spn_vel_machine_oper.valueChanged.disconnect()
            except Exception:
                pass
            self.spn_vel_machine_oper.valueChanged.connect(self.on_machine_ovr_spin)

        if hasattr(self, "sld_vel_spindle_oper"):
            try:
                self.sld_vel_spindle_oper.valueChanged.disconnect()
            except Exception:
                pass
            self.sld_vel_spindle_oper.valueChanged.connect(self._dbg_spindle_ovr_changed)

        if hasattr(self, "spn_vel_spindle_oper"):
            try:
                self.spn_vel_spindle_oper.valueChanged.disconnect()
            except Exception:
                pass
            self.spn_vel_spindle_oper.valueChanged.connect(self._dbg_spindle_ovr_spin_changed)

        self._apply_machine_override_pct(100)
        self._apply_spindle_override_pct(100)

        # JOG
        self._jog_busy = False
        self._jog_step_mm_default = 0.1

        self._jog_finish_timer = QtCore.QTimer(self)
        self._jog_finish_timer.timeout.connect(self._jog_finish_tick)
        self._jog_finish_axis = ""
        self._jog_finish_deadline = 0.0

        if hasattr(self, "btn_jog_x_plus"):
            self.btn_jog_x_plus.clicked.connect(lambda: self._jog_click("X", +1))
        if hasattr(self, "btn_jog_x_minus"):
            self.btn_jog_x_minus.clicked.connect(lambda: self._jog_click("X", -1))
        if hasattr(self, "btn_jog_z_plus"):
            self.btn_jog_z_plus.clicked.connect(lambda: self._jog_click("Z", +1))
        if hasattr(self, "btn_jog_z_minus"):
            self.btn_jog_z_minus.clicked.connect(lambda: self._jog_click("Z", -1))

        # JOG contínuo
        try:
            if hasattr(self, "btn_jog_x_plus"):
                self.btn_jog_x_plus.pressed.connect(lambda: self._jog_continuous_press("X", +1))
                self.btn_jog_x_plus.released.connect(lambda: self._jog_continuous_release("X"))
            if hasattr(self, "btn_jog_x_minus"):
                self.btn_jog_x_minus.pressed.connect(lambda: self._jog_continuous_press("X", -1))
                self.btn_jog_x_minus.released.connect(lambda: self._jog_continuous_release("X"))
            if hasattr(self, "btn_jog_z_plus"):
                self.btn_jog_z_plus.pressed.connect(lambda: self._jog_continuous_press("Z", +1))
                self.btn_jog_z_plus.released.connect(lambda: self._jog_continuous_release("Z"))
            if hasattr(self, "btn_jog_z_minus"):
                self.btn_jog_z_minus.pressed.connect(lambda: self._jog_continuous_press("Z", -1))
                self.btn_jog_z_minus.released.connect(lambda: self._jog_continuous_release("Z"))
        except Exception:
            pass

        # Modo JOG
        if hasattr(self, "btn_jog_mode"):
            self._jog_mode_options = ["Contínuo", "Incremental"]
            self._jog_mode_idx = 0
            try:
                self.btn_jog_mode.setText(self._jog_mode_options[self._jog_mode_idx])
            except Exception:
                pass
            try:
                self.btn_jog_mode.clicked.connect(self._on_btn_jog_mode_clicked)
            except Exception:
                pass
            try:
                self._on_jog_mode_changed(self._jog_mode_options[self._jog_mode_idx])
            except Exception:
                pass

        self._jog_step_mm = 0.1

        # Passo JOG
        if hasattr(self, "btn_jog_step"):
            self._jog_step_options = [10.0, 1.0, 0.5, 0.1, 0.01, 0.001]
            self._jog_step_idx = 0
            try:
                self.btn_jog_step.setText(f"{self._jog_step_options[self._jog_step_idx]:g} mm")
            except Exception:
                pass
            try:
                self.btn_jog_step.clicked.connect(self._on_btn_jog_step_clicked)
            except Exception:
                pass
            try:
                self._on_jog_step_changed(str(self._jog_step_options[self._jog_step_idx]))
            except Exception:
                pass

        # Velocidade JOG
        self._jog_speed_pct = 100
        self._jog_speed_max_mm_min = float(self._get_jog_max_mm_min_safe())

        if hasattr(self, "sld_vel_jog_oper"):
            self.sld_vel_jog_oper.setRange(0, 120)
            self.sld_vel_jog_oper.setValue(100)
        if hasattr(self, "spn_jog_speed"):
            self.spn_jog_speed.setRange(0, 120)
            self.spn_jog_speed.setValue(100)
            try:
                self.spn_jog_speed.setSuffix("%")
            except Exception:
                pass

        if hasattr(self, "sld_vel_jog_oper"):
            try:
                self.sld_vel_jog_oper.valueChanged.disconnect()
            except Exception:
                pass
            self.sld_vel_jog_oper.valueChanged.connect(self._on_jog_speed_slider_changed)
        if hasattr(self, "spn_jog_speed"):
            try:
                self.spn_jog_speed.valueChanged.disconnect()
            except Exception:
                pass
            self.spn_jog_speed.valueChanged.connect(self._on_jog_speed_spin_changed)

        self._apply_jog_speed_pct(100)

    # ══════════════════════════════════════════════════════════
    # OVERRIDES
    # ══════════════════════════════════════════════════════════

    def _apply_machine_override_pct(self, pct: int):
        try:
            pct_i = int(max(0, min(120, pct)))
            self._machine_ovr_pct = pct_i
            self.cmd.feedrate(pct_i / 100.0)
        except Exception as e:
            print(f"[ICEQ] feed override erro: {e}")

    def _sync_machine_widgets(self, pct: int):
        if hasattr(self, "sld_vel_machine_oper") and self.sld_vel_machine_oper.value() != pct:
            self.sld_vel_machine_oper.blockSignals(True)
            self.sld_vel_machine_oper.setValue(pct)
            self.sld_vel_machine_oper.blockSignals(False)
        if hasattr(self, "spn_vel_machine_oper") and self.spn_vel_machine_oper.value() != pct:
            self.spn_vel_machine_oper.blockSignals(True)
            self.spn_vel_machine_oper.setValue(pct)
            self.spn_vel_machine_oper.blockSignals(False)

    def on_machine_ovr_slider(self, value: int):
        pct = int(max(0, min(120, value)))
        self._sync_machine_widgets(pct)
        self._apply_machine_override_pct(pct)

    def on_machine_ovr_spin(self, value: int):
        pct = int(max(0, min(120, value)))
        self._sync_machine_widgets(pct)
        self._apply_machine_override_pct(pct)

    def on_spindle_ovr_slider(self, value):
        pct = self._clamp_pct(value, 0, 120)
        self._sync_spindle_widgets(pct)
        self._apply_spindle_override_pct(pct)

    def on_spindle_ovr_spin(self, value):
        pct = self._clamp_pct(value, 0, 120)
        self._sync_spindle_widgets(pct)
        self._apply_spindle_override_pct(pct)

    def _sync_spindle_widgets(self, pct):
        self._spindle_ovr_pct = pct
        if hasattr(self, "sld_vel_spindle_oper"):
            self.sld_vel_spindle_oper.blockSignals(True)
            self.sld_vel_spindle_oper.setValue(pct)
            self.sld_vel_spindle_oper.blockSignals(False)
        if hasattr(self, "spn_vel_spindle_oper"):
            self.spn_vel_spindle_oper.blockSignals(True)
            self.spn_vel_spindle_oper.setValue(pct)
            self.spn_vel_spindle_oper.blockSignals(False)

    def _apply_spindle_override_pct(self, pct):
        pct = self._clamp_pct(pct, 0, 120)
        scale = float(pct) / 100.0
        try:
            if hasattr(self.cmd, "spindleoverride"):
                self.cmd.spindleoverride(scale)
                return
        except Exception as e:
            print(f"[ICEQ] spindle override via cmd falhou: {e}")

    def _dbg_machine_ovr_changed(self, v):
        self.on_machine_ovr_slider(v)

    def _dbg_spindle_ovr_changed(self, v):
        self.on_spindle_ovr_slider(v)

    def _dbg_spindle_ovr_spin_changed(self, v):
        self.on_spindle_ovr_spin(v)

    def _clamp_pct(self, v, lo=0, hi=120):
        try:
            v = int(v)
        except Exception:
            v = 100
        return max(lo, min(hi, v))

    # ══════════════════════════════════════════════════════════
    # LED helpers
    # ══════════════════════════════════════════════════════════

    def set_led(self, frame, is_on):
        if is_on:
            frame.setStyleSheet("background-color: rgb(0, 255, 0); border: 1px solid black;")
        else:
            frame.setStyleSheet("background-color: rgb(255, 0, 0); border: 1px solid black;")

    def _set_state_label(self, widget_name: str, value: bool):
        if hasattr(self, widget_name):
            getattr(self, widget_name).setText("TRUE" if value else "FALSE")

    def _set_label_if_exists(self, attr_name, text):
        try:
            w = getattr(self, attr_name, None)
            if w is None:
                return
            if hasattr(w, "setText"):
                w.setText(text)
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════
    # INTERTRAVAMENTO VISUAL
    # ══════════════════════════════════════════════════════════

    def _blink_tick(self):
        try:
            self._blink_phase = not bool(getattr(self, "_blink_phase", False))
            self._update_interlock_visuals()
        except Exception:
            pass

    def _btn_qss_bg(self, rgb_text):
        return (
            "QPushButton{"
            f"background-color: {rgb_text};"
            "border: 1px solid rgb(80, 80, 80);"
            "padding: 0px; margin: 0px;"
            "}"
            "QPushButton:pressed{"
            f"background-color: {rgb_text};"
            "padding: 0px; margin: 0px;"
            "}"
        )

    def _btn_set_visual(self, btn_attr, mode):
        if not hasattr(self, btn_attr):
            return
        btn = getattr(self, btn_attr)
        try:
            sz = getattr(self, "_btn_size_default", {}).get(btn_attr, None)
            if sz:
                btn.setMinimumSize(sz[0], sz[1])
                btn.setMaximumSize(sz[2], sz[3])
        except Exception:
            pass

        base = self._btn_style_default.get(btn_attr, "")

        if mode == "default":
            btn.setStyleSheet(base)
        elif mode == "solid_green":
            btn.setStyleSheet(self._btn_qss_bg("rgb(0, 200, 0)"))
        elif mode == "blink_red":
            if bool(getattr(self, "_blink_phase", False)):
                btn.setStyleSheet(self._btn_qss_bg("rgb(220, 0, 0)"))
            else:
                btn.setStyleSheet(base)
        elif mode == "blink_yellow":
            if bool(getattr(self, "_blink_phase", False)):
                btn.setStyleSheet(self._btn_qss_bg("rgb(255, 200, 0)"))
            else:
                btn.setStyleSheet(base)
        else:
            btn.setStyleSheet(base)

    def _update_interlock_visuals(self):
        try:
            estop_active = bool(getattr(self.stat, "estop", False))
            enabled = bool(getattr(self.stat, "enabled", False))

            homed_all = False
            try:
                h = getattr(self.stat, "homed", None)
                if h is not None and len(h) >= 2:
                    homed_all = bool(h[0]) and bool(h[1])
            except Exception:
                homed_all = False

            program_active = False
            paused = False
            try:
                mode = getattr(self.stat, "task_mode", None)
                interp = getattr(self.stat, "interp_state", None)
                paused = bool(getattr(self.stat, "paused", False))
                program_active = (mode == linuxcnc.MODE_AUTO and interp != linuxcnc.INTERP_IDLE)
            except Exception:
                program_active = False
                paused = False

            if estop_active:
                self._btn_set_visual("btn_emergencia_bottom", "blink_red")
                self._btn_set_visual("btn_machine_off_top", "default")
                self._btn_set_visual("btn_ref_all", "default")
                self._btn_set_visual("btn_start_cycle", "default")
                return

            self._btn_set_visual("btn_emergencia_bottom", "solid_green")

            if not enabled:
                self._btn_set_visual("btn_machine_off_top", "blink_yellow")
                self._btn_set_visual("btn_ref_all", "default")
                self._btn_set_visual("btn_start_cycle", "default")
                return

            self._btn_set_visual("btn_machine_off_top", "solid_green")

            if bool(getattr(self, "_homing_busy", False)):
                self._btn_set_visual("btn_ref_all", "blink_yellow")
                self._btn_set_visual("btn_start_cycle", "default")
                return

            if not homed_all:
                self._btn_set_visual("btn_ref_all", "blink_yellow")
                self._btn_set_visual("btn_start_cycle", "default")
                return

            self._btn_set_visual("btn_ref_all", "solid_green")

            if program_active:
                if paused:
                    self._btn_set_visual("btn_start_cycle", "blink_yellow")
                else:
                    self._btn_set_visual("btn_start_cycle", "solid_green")
            else:
                self._btn_set_visual("btn_start_cycle", "default")

        except Exception:
            pass

    # ══════════════════════════════════════════════════════════
    # EMERGÊNCIA / MÁQUINA
    # ══════════════════════════════════════════════════════════

    def toggle_estop(self):
        try:
            self.stat.poll()
        except Exception as e:
            print(f"[ICEQ] toggle_estop: erro no stat.poll(): {e}")
            return
        estop = bool(self.stat.estop)
        if estop:
            self.cmd.state(linuxcnc.STATE_ESTOP_RESET)
        else:
            self.cmd.state(linuxcnc.STATE_ESTOP)

    def toggle_machine(self):
        try:
            self.stat.poll()
        except Exception as e:
            print(f"[ICEQ] toggle_machine: erro no stat.poll(): {e}")
            return
        estop = bool(self.stat.estop)
        enabled = bool(self.stat.enabled)
        if estop:
            return
        if not enabled:
            self.cmd.state(linuxcnc.STATE_ON)
        else:
            self.cmd.state(linuxcnc.STATE_OFF)

    def cycle_start_toggle(self):
        try:
            self.stat.poll()
        except Exception as e:
            print(f"[ICEQ] cycle_start: erro no stat.poll(): {e}")
            return

        estop   = bool(self.stat.estop)
        enabled = bool(self.stat.enabled)
        mode    = self.stat.task_mode
        interp  = self.stat.interp_state
        paused  = bool(self.stat.paused)

        if estop or not enabled:
            return

        if mode != linuxcnc.MODE_AUTO:
            try:
                self.cmd.mode(linuxcnc.MODE_AUTO)
                self.cmd.wait_complete()
            except Exception as e:
                print(f"[ICEQ] cycle_start: erro ao mudar para MODE_AUTO: {e}")
                return

        if paused:
            try:
                self.cmd.auto(linuxcnc.AUTO_RESUME)
            except Exception as e:
                print(f"[ICEQ] cycle_start: erro no AUTO_RESUME: {e}")
            return

        running = (interp != linuxcnc.INTERP_IDLE)
        if running:
            try:
                self.cmd.auto(linuxcnc.AUTO_PAUSE)
            except Exception as e:
                print(f"[ICEQ] cycle_start: erro no AUTO_PAUSE: {e}")
        else:
            try:
                self.cmd.auto(linuxcnc.AUTO_RUN, 0)
            except Exception as e:
                print(f"[ICEQ] cycle_start: erro no AUTO_RUN: {e}")

    def cycle_stop(self):
        try:
            self.cmd.abort()
        except Exception as e:
            print(f"[ICEQ] cycle_stop: erro no abort(): {e}")

    # ══════════════════════════════════════════════════════════
    # FERRAMENTAS T1..T16
    # ══════════════════════════════════════════════════════════

    def _init_tool_buttons(self):
        self._tool_btn_qss_default = {}
        self._tool_btn_active_last = None
        self._tool_buttons = []

        for n in range(1, 17):
            btn_t = getattr(self, f"btn_t{n}", None)
            if btn_t is None:
                continue
            btn_t.clicked.connect(lambda checked=False, nn=n: self._tool_request(nn))
            try:
                self._tool_btn_qss_default[int(n)] = str(btn_t.styleSheet() or "")
            except Exception:
                pass
            try:
                eff = QtWidgets.QGraphicsOpacityEffect(btn_t)
                eff.setOpacity(1.0)
                btn_t.setGraphicsEffect(eff)
            except Exception:
                eff = None
            self._tool_buttons.append((n, btn_t, eff))

        self._tool_active_last = None
        self._tool_buttons_locked_reason = ""
        self._tool_active_virtual = 0
        self._toolchange_busy = False
        self._toolchange_thread = None
        self._toolchange_req_tool = 0
        self._toolchange_last_ok = True
        self._toolchange_last_error = ""
        self._toolchange_started_ts = 0.0
        self._toolchange_timeout_s = 45.0
        self._toolchange_lock_min_s = 0.8
        self._mdi_pending_tool = 0

        self._set_tool_buttons_enabled(False)
        self._update_active_tool_label(None)

    def _update_active_tool_label(self, tool_num):
        lbl = getattr(self, "lbl_active_tool_title", None)
        if lbl is None:
            return
        if tool_num is None or int(tool_num) <= 0:
            lbl.setText("Ferramenta Ativa: --")
        else:
            lbl.setText(f"Ferramenta Ativa: T{int(tool_num)}")

    def _update_active_tool_button_style(self, active_tool):
        try:
            at = int(active_tool)
        except Exception:
            at = 0

        if at == getattr(self, "_tool_btn_active_last", None):
            return
        self._tool_btn_active_last = at

        for item in getattr(self, "_tool_buttons", []):
            try:
                if len(item) == 2:
                    n, btn = item
                else:
                    n, btn, _eff = item
                base = str(getattr(self, "_tool_btn_qss_default", {}).get(int(n), "") or "")
                if at > 0 and int(n) == at:
                    btn.setStyleSheet(base + "\nQPushButton{background-color: rgb(0, 160, 0); color: white; font-weight: bold;}")
                else:
                    btn.setStyleSheet(base)
            except Exception:
                pass

    def _set_tool_buttons_enabled(self, enabled: bool):
        ena = bool(enabled)
        for item in getattr(self, "_tool_buttons", []):
            try:
                if len(item) == 2:
                    n, btn = item
                    eff = None
                else:
                    n, btn, eff = item
                btn.setEnabled(ena)
                btn.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, not ena)
                if eff is not None:
                    eff.setOpacity(1.0 if ena else 0.25)
            except Exception:
                pass

    def _toolchange_hw_active(self) -> bool:
        pins = ("motion.digital-out-06", "motion.digital-out-07")
        for p in pins:
            v = self._hal_bit(p)
            if v is not None and bool(v):
                return True
        return False

    def _compute_tools_interlock(self):
        try:
            self.stat.poll()
        except Exception:
            return False, "Sem status"

        if bool(getattr(self, "_toolchange_busy", False)):
            return False, "Troca em andamento"
        try:
            if bool(self._toolchange_hw_active()):
                return False, "Troca em andamento (HW)"
        except Exception:
            pass

        try:
            estop = bool(getattr(self.stat, "estop", 0))
        except Exception:
            estop = False
        try:
            machine_on = bool(getattr(self.stat, "enabled", 0))
        except Exception:
            machine_on = False

        if estop:
            return False, "E-Stop"
        if not machine_on:
            return False, "Maquina OFF"

        try:
            homed_x = bool(self.stat.homed[0]) if hasattr(self.stat, "homed") else False
            homed_z = bool(self.stat.homed[1]) if hasattr(self.stat, "homed") else False
            if not (homed_x and homed_z):
                return False, "Nao referenciada (X/Z)"
        except Exception:
            pass

        if bool(getattr(self, "_homing_busy", False)):
            return False, "Referenciando"

        try:
            task_mode = int(getattr(self.stat, "task_mode", -1))
            interp_state = int(getattr(self.stat, "interp_state", -1))
            mode_auto = int(getattr(linuxcnc, "MODE_AUTO", 2))
            interp_idle = int(getattr(linuxcnc, "INTERP_IDLE", 1))
            if task_mode == mode_auto and interp_state != -1 and interp_state != interp_idle:
                return False, "Programa em execucao"
        except Exception:
            pass

        return True, ""

    def _tool_request(self, tool_num: int):
        ok, reason = self._compute_tools_interlock()
        if not ok:
            print(f"[ICEQ] Troca bloqueada: {reason}")
            return
        if bool(getattr(self, "_toolchange_busy", False)):
            return
        try:
            self.stat.poll()
            cur = int(getattr(self.stat, "tool_in_spindle", 0))
            if int(cur) == int(tool_num):
                return
        except Exception:
            pass

        self._toolchange_started_ts = time.time()
        self._set_tool_buttons_enabled(False)
        try:
            QtWidgets.QApplication.processEvents()
        except Exception:
            pass

        self._toolchange_busy = True
        t = threading.Thread(
            target=self._toolchange_button_worker,
            args=(int(tool_num),),
            daemon=True
        )
        t.start()

    def _toolchange_button_worker(self, tool_num: int):
        ok = False
        err = ""
        try:
            ok, err = self._run_mdi_command(f"T{int(tool_num)} M6")
            if ok:
                self._tool_active_virtual = int(tool_num)
        except Exception as e:
            ok = False
            err = str(e)
        finally:
            try:
                self._toolchange_last_ok = bool(ok)
                self._toolchange_last_error = str(err or "")
            except Exception:
                pass
            try:
                self._toolchange_busy = False
            except Exception:
                pass
            if not ok:
                print(f"[ICEQ] ERRO troca ferramenta (BOTAO): {err}")

    def _update_tools_ui_tick(self):
        try:
            active_tool = int(getattr(self.stat, "tool_in_spindle", 0))
        except Exception:
            active_tool = 0
        if int(active_tool) <= 0:
            try:
                active_tool = int(getattr(self, "_tool_active_virtual", 0))
            except Exception:
                active_tool = 0

        try:
            if active_tool != getattr(self, "_tool_active_last", None):
                self._tool_active_last = active_tool
                self._update_active_tool_label(active_tool)
        except Exception:
            pass

        try:
            self._update_active_tool_button_style(active_tool)
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════
    # UPDATE STATUS PANEL
    # ══════════════════════════════════════════════════════════

    def update_status_panel(self):
        try:
            self.stat.poll()

            # Nome do arquivo G-code
            try:
                gcode_file = self.stat.file
                display_name = "Nenhum Gcode carregado" if not gcode_file else os.path.basename(gcode_file)
                text_html = f"<b>{display_name}</b>"
                if hasattr(self, "lbl_program_name"):
                    self.lbl_program_name.setText(text_html)
                if hasattr(self, "lbl_program_name2"):
                    self.lbl_program_name2.setText(text_html)
            except Exception:
                pass

            # Preview 2D auto-reload
            try:
                cur_file = str(getattr(self.stat, "file", "") or "")
                if cur_file and cur_file != self._preview2d_last_stat_file:
                    self._preview2d_last_stat_file = cur_file
                    self.preview2d.ensure_program_loaded(cur_file)
            except Exception:
                pass

            try:
                self._update_program_name_labels(str(getattr(self.stat, "file", "") or ""))
            except Exception:
                pass

            # Preview marcador posição
            try:
                self.preview2d.tick_live(self.stat)
            except Exception:
                pass
            try:
                self.preview2d.tick_program_line(self.stat)
            except Exception:
                pass

        except Exception as e:
            print(f"[ICEQ] update_status_panel: erro no stat.poll(): {e}")
            return

        estop   = bool(self.stat.estop)
        enabled = bool(self.stat.enabled)

        # JOG contínuo safety
        try:
            if bool(getattr(self, "_jog_cont_active", False)):
                ok, _reason = self._jog_machine_ready()
                if not ok:
                    ax = str(getattr(self, "_jog_cont_axis", "") or "")
                    if ax in ("X", "Z"):
                        self._jog_continuous_release(ax)
        except Exception:
            pass

        # LEDs EMERGÊNCIA / MACHINE ON
        emerg_ok = not estop
        self.set_led(self.led_maint_sig_emerg, emerg_ok)
        self.set_led(self.led_emerg, emerg_ok)
        self.lbl_maint_sig_emerg_state.setText("TRUE" if emerg_ok else "FALSE")

        machine_on = enabled and not estop
        self.set_led(self.led_maint_sig_machine_on, machine_on)
        self.set_led(self.led_machine, machine_on)
        self.lbl_maint_sig_machine_on_state.setText("TRUE" if machine_on else "FALSE")

        # Early exit durante troca
        try:
            if bool(getattr(self, "_toolchange_busy", False)) or bool(self._toolchange_hw_active()):
                self._update_tools_ui_tick()
                return
        except Exception:
            pass

        mode   = self.stat.task_mode
        interp = self.stat.interp_state
        paused = bool(self.stat.paused)

        # Amarração spindle/coolant
        try:
            machine_ready = (not estop and enabled)
            auto_active = (mode == linuxcnc.MODE_AUTO and interp != linuxcnc.INTERP_IDLE)
            spindle_enable = (machine_ready and (not auto_active))
            coolant_enable = machine_ready

            for btn_name in ("btn_spindle_rpm_plus", "btn_spindle_rpm_minus",
                             "btn_spindle_cw", "btn_spindle_ccw", "btn_spindle_stop"):
                if hasattr(self, btn_name):
                    getattr(self, btn_name).setEnabled(spindle_enable)
            if hasattr(self, "btn_refri_button"):
                self.btn_refri_button.setEnabled(coolant_enable)

            self._update_spindle_rpm_label()
        except Exception as e:
            print(f"[ICEQ] amarração spindle/coolant erro: {e}")

        # LEDs spindle/coolant
        try:
            spindle_dir = 0
            spindle_on  = False
            coolant_on  = False

            try:
                sp0 = self.stat.spindle[0]
                if hasattr(sp0, "get"):
                    spindle_on = bool(sp0.get("enabled", False))
                    spindle_dir = int(sp0.get("direction", 0) or 0)
                elif hasattr(sp0, "enabled"):
                    spindle_on = bool(getattr(sp0, "enabled", False))
                    spindle_dir = int(getattr(sp0, "direction", 0) or 0)
            except Exception:
                pass

            cw = (self._hal_bit("spindle.0.forward") or False)
            ccw = (self._hal_bit("spindle.0.reverse") or False)
            on_hal = (self._hal_bit("spindle.0.on") or False)
            if not on_hal:
                on_hal = bool(cw or ccw)
            if on_hal and cw and not ccw:
                spindle_on = True; spindle_dir = 1
            elif on_hal and ccw and not cw:
                spindle_on = True; spindle_dir = -1

            if not spindle_on:
                dir_int = int(getattr(self, "_spindle_dir", 0))
                rpm_sp = int(abs(getattr(self, "_spindle_rpm_setpoint", 0)))
                if dir_int != 0 and rpm_sp > 0:
                    spindle_on = True
                    spindle_dir = dir_int

            coolant_on = bool(self._get_coolant_on_safe())

            if hasattr(self, "led_spindle"):
                self.set_led(self.led_spindle, spindle_on)
            if hasattr(self, "led_coolant"):
                self.set_led(self.led_coolant, coolant_on)

            for led_name, val in [
                ("led_maint_sig_spindle_cw", spindle_dir > 0),
                ("led_maint_sig_spindle_ccw", spindle_dir < 0),
                ("led_maint_sig_spindle_stop", not spindle_on),
                ("led_maint_sig_coolant", coolant_on),
            ]:
                if hasattr(self, led_name):
                    self.set_led(getattr(self, led_name), val)

            self._set_state_label("lbl_maint_sig_spindle_cw_state",   spindle_dir > 0)
            self._set_state_label("lbl_maint_sig_spindle_ccw_state",  spindle_dir < 0)
            self._set_state_label("lbl_maint_sig_spindle_stop_state", not spindle_on)
            self._set_state_label("lbl_maint_sig_coolant_state",      coolant_on)

        except Exception as e:
            print(f"[ICEQ] LEDs spindle/coolant erro: {e}")

        # Monitor sinais adicionais (spindle ON, AT SPEED, etc.)
        try:
            v_spindle_on = bool(
                self._hal_bit("spindle.0.on") or
                self._hal_bit("halui.spindle.0.is-on") or
                spindle_on
            )
            cmd_rpm = abs(float(self._hal_float("spindle.0.speed-out") or 0.0))
            if cmd_rpm <= 0.0:
                cmd_rpm = abs(float(self._hal_float("spindle.0.speed-out-abs") or 0.0))

            sp_at_speed = False
            fb_rps = abs(float(self._hal_float("spindle.0.speed-in") or 0.0))
            fb_rpm = fb_rps * 60.0
            if v_spindle_on and cmd_rpm > 0.0 and fb_rpm > 0.0:
                tol = max(self._sp_at_speed_tol_abs_rpm, self._sp_at_speed_tol_pct * cmd_rpm)
                if abs(cmd_rpm - fb_rpm) <= tol:
                    sp_at_speed = True

            if sp_at_speed:
                self._sp_at_speed_cnt = min(self._sp_at_speed_cnt + 1, 9999)
            else:
                self._sp_at_speed_cnt = 0
            v_spindle_at_speed = bool(self._sp_at_speed_cnt >= self._sp_at_speed_required_hits)

            rpmfb_active = (abs(float(self._hal_float("spindle.0.speed-in") or 0.0)) > 0.1)
            v_spindle_fault = bool(self._hal_bit("spindle.0.amp-fault-in") or False)

            for led_name, val in [
                ("led_maint_sig_spindle_on", v_spindle_on),
                ("led_maint_sig_spindle_at_speed", v_spindle_at_speed),
                ("led_maint_sig_rpm_fb_active", rpmfb_active),
                ("led_maint_sig_spindle_fault", v_spindle_fault),
            ]:
                if hasattr(self, led_name):
                    self.set_led(getattr(self, led_name), val)

            self._set_state_label("lbl_maint_sig_spindle_on_state", v_spindle_on)
            self._set_state_label("lbl_maint_sig_spindle_at_speed_state", v_spindle_at_speed)
            self._set_state_label("lbl_maint_sig_rpm_fb_active_state", rpmfb_active)
            self._set_state_label("lbl_maint_sig_spindle_fault_state", v_spindle_fault)

        except Exception:
            pass

        # Monitor torre/toolchange
        try:
            v_tc_active = bool(getattr(self, "_toolchange_busy", False)) or bool(self._toolchange_hw_active())
            try:
                tool_stat = int(getattr(self.stat, "tool_in_spindle", 0) or 0)
            except Exception:
                tool_stat = 0
            tool_now = tool_stat if tool_stat > 0 else int(getattr(self, "_tool_active_virtual", 0) or 0)
            v_tool_active = bool(tool_now > 0)

            v_sol = bool(self._hal_bit_multi(["motion.digital-out-06", "motion.digital-out-6"]))
            v_cw_t = False
            v_ccw_t = False

            for led_name, val in [
                ("led_maint_sig_toolchange_active", v_tc_active),
                ("led_maint_sig_tool_active", v_tool_active),
                ("led_maint_sig_turret_solenoid", v_sol),
                ("led_maint_sig_turret_cw", v_cw_t),
                ("led_maint_sig_turret_ccw", v_ccw_t),
            ]:
                if hasattr(self, led_name):
                    self.set_led(getattr(self, led_name), val)

            self._set_state_label("lbl_maint_sig_toolchange_active_state", v_tc_active)
            self._set_state_label("lbl_maint_sig_tool_active_state", v_tool_active)
            self._set_state_label("lbl_maint_sig_turret_solenoid_state", v_sol)
        except Exception:
            pass

        # Ferramentas UI
        self._update_tools_ui_tick()

        # Intertravamento ferramentas
        try:
            ts = float(getattr(self, "_toolchange_started_ts", 0.0) or 0.0)
            min_s = float(getattr(self, "_toolchange_lock_min_s", 0.0) or 0.0)
            if ts > 0.0 and min_s > 0.0 and (time.time() - ts) < min_s:
                pass
            else:
                if not bool(getattr(self, "_toolchange_busy", False)) and not bool(self._toolchange_hw_active()):
                    ok, reason = self._compute_tools_interlock()
                    self._tool_buttons_locked_reason = reason
                    self._set_tool_buttons_enabled(bool(ok))
        except Exception:
            self._set_tool_buttons_enabled(False)

        # RPM spindle (rodapé) — usa encoder como fonte primária
        try:
            # 1. RPM real via encoder (spindle.0.speed-in = RPS → × 60 = RPM)
            fb_rps = abs(float(self._hal_float("spindle.0.speed-in") or 0.0))
            rpm_encoder = fb_rps * 60.0

            # 2. Estado ON/OFF via stat
            sp_on = False
            sp_dir = 0
            try:
                sp = self.stat.spindle[0]
                sp_enabled = bool(sp.get("enabled", 0))
                sp_dir = int(sp.get("direction", 0))
                sp_on = (sp_enabled and sp_dir != 0)
            except Exception:
                pass

            # 3. Se encoder retorna RPM válido, usa ele; senão usa setpoint
            if rpm_encoder > 5.0:
                rpm_disp = rpm_encoder
                sp_on = True
            elif sp_on:
                rpm_disp = abs(float(self._spindle_rpm_setpoint))
            else:
                rpm_disp = 0.0

            if sp_on and rpm_disp > 0.1:
                self._set_label_if_exists("lbl_spindle_rpm", f"{rpm_disp:.0f} RPM")
                self._update_spindle_rpm_bar(rpm_disp)
            else:
                self._set_label_if_exists("lbl_spindle_rpm", "0 RPM")
                self._update_spindle_rpm_bar(0.0)
        except Exception:
            pass

        # Progresso
        try:
            if mode == linuxcnc.MODE_AUTO and interp != linuxcnc.INTERP_IDLE:
                if self._gcode_total_lines > 0:
                    cur_line = max(0, int(getattr(self.stat, "current_line", 0)))
                    pct = int(min(100, max(0, (cur_line * 100) / float(self._gcode_total_lines))))
                    self._last_progress_pct = pct
                    if hasattr(self, "prg_cycle_top"):
                        self.prg_cycle_top.setValue(pct)
                        self.prg_cycle_top.setFormat(f"{pct}%")
            else:
                if hasattr(self, "prg_cycle_top"):
                    self.prg_cycle_top.setValue(100)
                    self.prg_cycle_top.setFormat("100%")
        except Exception:
            pass

        # Debug terminal
        cur = (int(mode), int(interp), bool(paused))
        if cur != self._dbg_last_status:
            self._dbg_last_status = cur
            print(f"[ICEQ] status: mode={mode} interp={interp} paused={paused}")

        # LEDs START/PAUSE
        if mode != linuxcnc.MODE_AUTO:
            program_running = False
            program_paused  = False
        else:
            program_paused  = paused
            program_running = (not paused and interp != linuxcnc.INTERP_IDLE)

        self.set_led(self.led_maint_sig_start, program_running)
        self.set_led(self.led_maint_sig_pause, program_paused)

        if hasattr(self, "lbl_maint_sig_start_state"):
            self.lbl_maint_sig_start_state.setText("TRUE" if program_running else "FALSE")
        if hasattr(self, "lbl_maint_sig_pause_state"):
            self.lbl_maint_sig_pause_state.setText("TRUE" if program_paused else "FALSE")

        # LED PROGRAMA rodapé
        if program_running:
            color = "rgb(0, 255, 0)"
        elif program_paused:
            color = "rgb(255, 255, 0)"
        else:
            color = "rgb(255, 0, 0)"
        self.led_program.setStyleSheet(f"background-color: {color}; border: 1px solid black;")

        # ── [FIX-2] Cabeçalho X/Z/VEL com índices corretos ──
        try:
            lu = float(getattr(self.stat, "linear_units", 1.0))

            def to_mm(v):
                try:
                    v = float(v)
                    if lu < 0.999:
                        return v / lu
                    elif lu > 1.001:
                        return v * lu
                    return v
                except Exception:
                    return 0.0

            pos = getattr(self.stat, "actual_position", None)
            if pos is None:
                pos = getattr(self.stat, "position", None)

            if pos is not None:
                x_mm = to_mm(pos[0])       # joint 0 = X
                z_mm = to_mm(pos[1])       # joint 1 = Z  [FIX-2]
                x_disp = x_mm * 2.0        # diâmetro
                if hasattr(self, "lbl_hdr_x"):
                    self.lbl_hdr_x.setText(f"X: {x_disp:.3f}")
                if hasattr(self, "lbl_hdr_z"):
                    self.lbl_hdr_z.setText(f"Z: {z_mm:.3f}")

            v = getattr(self.stat, "current_vel", None)
            if v is not None:
                v_mm_min = to_mm(v) * 60.0
                if hasattr(self, "lbl_hdr_vel"):
                    self.lbl_hdr_vel.setText(f"VEL: {v_mm_min:.2f} mm/min")

        except Exception as e:
            print(f"[ICEQ] cabecalho X/Z/VEL erro: {e}")

        # RPM máximo do INI
        try:
            import configparser
            ini_path = (
                os.environ.get("INI_FILE_NAME")
                or os.environ.get("EMC2_INI_FILE_NAME")
                or ""
            )
            rpm_max_val = None

            def _parse_ini_number(raw):
                if raw is None:
                    return None
                s = str(raw).strip().split(";")[0].split("#")[0].strip()
                m = re.search(r"[-+]?\d+(?:[.,]\d+)?", s)
                if not m:
                    return None
                try:
                    return float(m.group(0).replace(",", "."))
                except Exception:
                    return None

            if ini_path:
                cfg = configparser.RawConfigParser(inline_comment_prefixes=(";", "#"), strict=False)
                if cfg.read(ini_path):
                    for sec in ("SPINDLE_0", "spindle_0", "SPINDLE", "spindle"):
                        if cfg.has_section(sec):
                            for key in ("MAX_RPM", "MAX_SPEED", "MAX_VELOCITY", "MAX_OUTPUT"):
                                fv = _parse_ini_number(cfg.get(sec, key, fallback=None))
                                if fv is not None and fv > 0:
                                    rpm_max_val = int(round(fv))
                                    break
                            break

            if rpm_max_val and rpm_max_val > 0:
                self._spindle_rpm_max_val = rpm_max_val
                self.lbl_rpm_max.setText(f"{rpm_max_val} rpm/max")
            else:
                self._spindle_rpm_max_val = 0
                self.lbl_rpm_max.setText("RPM máx indef.")
        except Exception:
            self._spindle_rpm_max_val = 0
            self.lbl_rpm_max.setText("RPM máx indef.")

        # VEL máquina
        try:
            vmax_mm_min = float(self._get_traj_max_mm_min_safe() or 0.0)
            if hasattr(self, "lbl_vel_machine_oper"):
                if vmax_mm_min > 0.0:
                    self.lbl_vel_machine_oper.setText(f"{int(round(vmax_mm_min))} mm/min máx")
                else:
                    self.lbl_vel_machine_oper.setText("VEL máx indef.")
        except Exception:
            pass

        # VEL atual
        try:
            if hasattr(self, "lbl_vel_machine"):
                vmax_mm_min = float(self._get_traj_max_mm_min_safe() or 0.0)
                scale = None
                try:
                    rr = getattr(self.stat, "rapidrate", None)
                    fr = getattr(self.stat, "feedrate", None)
                    if isinstance(rr, (int, float)) and rr is not None:
                        scale = float(rr)
                    elif isinstance(fr, (int, float)) and fr is not None:
                        scale = float(fr)
                except Exception:
                    pass
                if scale is None:
                    pct = int(getattr(self, "_machine_ovr_pct", 100))
                    scale = float(pct) / 100.0
                if vmax_mm_min > 0.0 and scale is not None:
                    vcur = vmax_mm_min * max(0.0, scale)
                    self.lbl_vel_machine.setText(f"{vcur:.2f} mm/min")
                else:
                    self.lbl_vel_machine.setText("VEL atual indef.")
        except Exception:
            pass

        # Destaque linha G-code
        try:
            if mode == linuxcnc.MODE_AUTO and interp != linuxcnc.INTERP_IDLE:
                cl = int(self.stat.current_line)
                if paused:
                    current_line = max(0, cl - 1)
                elif interp == linuxcnc.INTERP_WAITING:
                    current_line = max(0, cl - 1)
                else:
                    current_line = max(0, cl)
                if hasattr(self, "txt_editor"):
                    self._highlight_gcode_line(self.txt_editor, current_line)
                if hasattr(self, "txt_gcode_view"):
                    self._highlight_gcode_line(self.txt_gcode_view, current_line)
            else:
                if hasattr(self, "txt_editor"):
                    self._clear_gcode_highlight(self.txt_editor)
                if hasattr(self, "txt_gcode_view"):
                    self._clear_gcode_highlight(self.txt_gcode_view)
        except Exception:
            pass

        # Tempo do ciclo
        try:
            program_active = (mode == linuxcnc.MODE_AUTO and interp != linuxcnc.INTERP_IDLE)
            program_running_now = (program_active and not paused)

            if program_running_now and not self._cycle_running:
                self._cycle_running = True
                self._cycle_start_ts = time.monotonic()
                self._cycle_last_elapsed = 0.0

            if self._cycle_running and self._cycle_start_ts is not None:
                elapsed_now = time.monotonic() - self._cycle_start_ts
            else:
                elapsed_now = self._cycle_last_elapsed

            if self._cycle_running and not program_active:
                self._cycle_running = False
                self._cycle_last_elapsed = elapsed_now

            if hasattr(self, "lbl_cycle_time_top"):
                t_disp = elapsed_now if self._cycle_running else self._cycle_last_elapsed
                self.lbl_cycle_time_top.setText(self._format_hms(t_disp))

            if hasattr(self, "prg_cycle_top"):
                if program_active and self._gcode_total_lines and self._gcode_total_lines > 0:
                    cur = int(getattr(self.stat, "current_line", 0))
                    pct = min(100.0, max(0.0, (float(cur) / float(self._gcode_total_lines)) * 100.0))
                    self.prg_cycle_top.setValue(int(pct))
                elif not self._gcode_total_lines:
                    self.prg_cycle_top.setValue(0)
        except Exception:
            pass

        # Intertravamento visual dos botões
        try:
            self._update_interlock_visuals()
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════
    # HIGHLIGHT G-CODE
    # ══════════════════════════════════════════════════════════

    def _highlight_gcode_line(self, widget, line_index):
        try:
            if widget is None:
                return
            doc = widget.document()
            block = doc.findBlockByNumber(int(line_index))
            if not block.isValid():
                return
            cursor = widget.textCursor()
            cursor.setPosition(block.position())
            cursor.select(cursor.LineUnderCursor)
            widget.setTextCursor(cursor)
            widget.ensureCursorVisible()
        except Exception:
            pass

    def _clear_gcode_highlight(self, widget):
        try:
            if widget is None:
                return
            cursor = widget.textCursor()
            cursor.clearSelection()
            widget.setTextCursor(cursor)
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════
    # ABRIR PROGRAMA
    # ══════════════════════════════════════════════════════════

    def open_program(self):
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "Abrir Programa G-code",
            "/home/iceq/linuxcnc/nc_files",
            "G-code (*.ngc *.nc *.tap *.gcode);;Todos (*.*)"
        )
        if not filename:
            return

        try:
            self.cmd.program_open(filename)
        except Exception as e:
            print(f"[ICEQ] Erro ao abrir programa no LinuxCNC: {e}")
            return

        try:
            with open(filename, 'r') as f:
                conteudo = f.read()
            linhas = conteudo.splitlines()
            self._gcode_total_lines = max(1, len(linhas))
            self._last_progress_pct = 0
            if hasattr(self, "prg_cycle_top"):
                self.prg_cycle_top.setValue(0)
                self.prg_cycle_top.setFormat("0%")
        except Exception as e:
            print(f"[ICEQ] Erro ao ler arquivo '{filename}': {e}")
            return

        self._program_loaded_path = filename
        self._update_program_name_labels(filename)

        if hasattr(self, "txt_gcode_view"):
            try:
                self.txt_gcode_view.setPlainText(conteudo)
            except Exception:
                pass
        if hasattr(self, "txt_editor"):
            try:
                self.txt_editor.setPlainText(conteudo)
            except Exception:
                pass

        try:
            self.preview2d.ensure_program_loaded(filename)
            self._preview2d_last_stat_file = filename
        except Exception:
            pass

    def _program_open_path(self, file_path):
        try:
            if not file_path:
                return
            self._program_loaded_path = str(file_path)
            with self._cmd_lock:
                self.cmd.program_open(str(file_path))
                try:
                    self.cmd.wait_complete()
                except Exception:
                    pass
            self._update_program_name_labels(str(file_path))
        except Exception as e:
            print(f"[ICEQ][ERRO] Falha ao carregar programa: {e}")

    def _editor_save(self):
        try:
            text = self.txt_editor.toPlainText()
            if not text.strip():
                return

            if self._editor_current_file:
                with open(self._editor_current_file, "w", encoding="utf-8") as f:
                    f.write(text)
                self._program_open_path(self._editor_current_file)
                self._program_refresh_ui_after_load(self._editor_current_file, text=text)
                return

            file_path, _ = QFileDialog.getSaveFileName(
                self, "Salvar G-code",
                "/home/iceq/linuxcnc/nc_files",
                "G-code (*.ngc *.nc *.gcode)"
            )
            if not file_path:
                return
            if not file_path.lower().endswith((".ngc", ".nc", ".gcode")):
                file_path += ".ngc"

            with open(file_path, "w", encoding="utf-8") as f:
                f.write(text)
            self._editor_current_file = file_path
            self._program_open_path(file_path)
            self._program_refresh_ui_after_load(file_path, text=text)
        except Exception as e:
            print(f"[ICEQ][ERRO] Falha ao salvar arquivo: {e}")

    def _update_program_name_labels(self, file_path):
        name = "Nenhum Gcode carregado" if not file_path else os.path.basename(file_path)
        text = f"<b>{name}</b>"
        if hasattr(self, "lbl_program_name"):
            self.lbl_program_name.setText(text)
        if hasattr(self, "lbl_program_name2"):
            self.lbl_program_name2.setText(text)

    def _program_refresh_ui_after_load(self, file_path, text=None):
        try:
            if not file_path:
                return
            if text is not None:
                if hasattr(self, "txt_gcode_view"):
                    try:
                        self.txt_gcode_view.setPlainText(str(text))
                    except Exception:
                        pass
            if hasattr(self, "preview2d") and self.preview2d:
                try:
                    self.preview2d.ensure_program_loaded(str(file_path))
                    self._preview2d_last_stat_file = str(file_path)
                except Exception:
                    pass
        except Exception as e:
            print(f"[ICEQ] Erro em _program_refresh_ui_after_load: {e}")

    # ══════════════════════════════════════════════════════════
    # MDI
    # ══════════════════════════════════════════════════════════

    def _mdi_history_file_path(self):
        ini_path = os.environ.get("INI_FILE_NAME") or os.environ.get("EMC2_INI_FILE_NAME") or ""
        base_dir = ""
        if ini_path:
            try:
                base_dir = os.path.dirname(str(ini_path))
            except Exception:
                base_dir = ""
        if not base_dir:
            base_dir = os.path.join(os.path.expanduser("~"), ".iceq")
        try:
            os.makedirs(base_dir, exist_ok=True)
        except Exception:
            pass
        return os.path.join(base_dir, "iceq_mdi_history.txt")

    def _mdi_history_load(self):
        if bool(getattr(self, "_mdi_history_loaded", False)):
            return
        if not hasattr(self, "txt_mdi_history"):
            return
        path = self._mdi_history_file_path()
        try:
            if not os.path.isfile(path):
                return
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = [ln.rstrip("\n\r") for ln in f.readlines()]
            max_lines = int(getattr(self, "_mdi_hist_max_lines", 400) or 400)
            if max_lines > 0 and len(lines) > max_lines:
                lines = lines[-max_lines:]
            txt = "\n".join([ln for ln in lines if ln])
            try:
                self.txt_mdi_history.setPlainText(txt)
            except Exception:
                self.txt_mdi_history.setText(txt)
            try:
                sb = self.txt_mdi_history.verticalScrollBar()
                sb.setValue(sb.maximum())
            except Exception:
                pass
        finally:
            self._mdi_history_loaded = True

    def _mdi_history_append_disk(self, text):
        if text is None:
            return
        try:
            path = self._mdi_history_file_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(str(text).replace("\r", "") + "\n")
        except Exception:
            pass

    def _append_mdi_history(self, text):
        if not hasattr(self, "txt_mdi_history"):
            return
        try:
            self.txt_mdi_history.appendPlainText(text)
            try:
                sb = self.txt_mdi_history.verticalScrollBar()
                sb.setValue(sb.maximum())
            except Exception:
                pass
        except Exception:
            try:
                self.txt_mdi_history.append(text)
            except Exception:
                pass
        try:
            self._mdi_history_append_disk(text)
        except Exception:
            pass

    def eventFilter(self, obj, event):
        try:
            if hasattr(self, "txt_mdi_history"):
                w = self.txt_mdi_history
                if obj is w or (hasattr(w, "viewport") and obj is w.viewport()):
                    if event.type() == QtCore.QEvent.MouseButtonRelease:
                        try:
                            if event.button() == QtCore.Qt.LeftButton:
                                self._mdi_history_pick_to_entry(event)
                        except Exception:
                            pass
        except Exception:
            pass
        return super().eventFilter(obj, event)

    def _mdi_history_pick_to_entry(self, event=None, from_viewport=False):
        if not hasattr(self, "txt_mdi_history"):
            return
        if not hasattr(self, "txt_mdi_entry"):
            return
        try:
            w = self.txt_mdi_history
            pos = event.pos() if event is not None else None
            if pos is None:
                return
            cur = w.cursorForPosition(pos)
            cur.select(QtGui.QTextCursor.LineUnderCursor)
            line = str(cur.selectedText()).strip()
            if not line or line.upper().startswith("ERR:"):
                return
            self.txt_mdi_entry.setText(line)
            self.txt_mdi_entry.setFocus()
            try:
                self.txt_mdi_entry.selectAll()
            except Exception:
                pass
        except Exception:
            pass

    def _run_mdi_command(self, cmd_text):
        try:
            with self._cmd_lock:
                self.cmd.mode(linuxcnc.MODE_MDI)
                self.cmd.wait_complete()
                self.cmd.mdi(cmd_text)
                self.cmd.wait_complete()
            return True, ""
        except Exception as e:
            return False, str(e)

    def on_mdi_send(self):
        if not hasattr(self, "txt_mdi_entry"):
            return
        try:
            cmd_text = self.txt_mdi_entry.text().strip()
        except Exception:
            return
        if not cmd_text:
            return

        self._append_mdi_history(f"{cmd_text}")
        if bool(getattr(self, "_mdi_busy", False)):
            self._append_mdi_history("ERR: MDI ocupado (aguarde finalizar)")
            return

        self._mdi_busy = True
        try:
            self.txt_mdi_entry.clear()
            self.txt_mdi_entry.setFocus()
        except Exception:
            pass

        self._start_mdi_fsm(cmd_text)

    def _start_mdi_fsm(self, cmd_text: str):
        try:
            self._mdi_last_cmd = str(cmd_text)
            s = str(cmd_text).strip().upper()
            self._mdi_is_toolchange = bool(re.search(r"\bM\s*6\b", s))
            try:
                self.stat.poll()
                self._mdi_prev_mode = int(getattr(self.stat, "task_mode", linuxcnc.MODE_MANUAL))
            except Exception:
                self._mdi_prev_mode = int(linuxcnc.MODE_MANUAL)

            self._mdi_fsm_state = "MDI_MODE"
            self._mdi_fsm_sent = False
            self._mdi_sent_ts = 0.0
            self._mdi_deadline = time.time() + (120.0 if bool(getattr(self, "_mdi_is_toolchange", False)) else 60.0)

            if self._mdi_fsm_timer is None:
                self._mdi_fsm_timer = QtCore.QTimer(self)
                self._mdi_fsm_timer.timeout.connect(self._mdi_fsm_tick)
            self._mdi_fsm_timer.start(50)
        except Exception as e:
            self._append_mdi_history(f"ERR: falha iniciando MDI FSM: {e}")
            self._mdi_busy = False

    def _mdi_fsm_tick(self):
        try:
            if time.time() > float(getattr(self, "_mdi_deadline", 0.0) or 0.0):
                raise RuntimeError("timeout aguardando fim do MDI (FSM)")
            try:
                self.stat.poll()
            except Exception:
                pass

            st = str(getattr(self, "_mdi_fsm_state", ""))

            if st == "MDI_MODE":
                try:
                    self.cmd.mode(linuxcnc.MODE_MDI)
                except Exception:
                    pass
                try:
                    if int(getattr(self.stat, "task_mode", -1)) == int(linuxcnc.MODE_MDI):
                        self._mdi_fsm_state = "SEND"
                        self._mdi_fsm_sent = False
                    else:
                        return
                except Exception:
                    self._mdi_fsm_state = "SEND"
                    self._mdi_fsm_sent = False
                    return

            if st == "SEND":
                if not bool(getattr(self, "_mdi_fsm_sent", False)):
                    try:
                        with self._cmd_lock:
                            self.cmd.mdi(str(getattr(self, "_mdi_last_cmd", "")))
                    except Exception:
                        raise
                    self._mdi_fsm_sent = True
                    self._mdi_sent_ts = time.time()
                    self._mdi_fsm_state = "WAIT_IDLE"
                return

            if st == "WAIT_IDLE":
                try:
                    interp = int(getattr(self.stat, "interp_state", linuxcnc.INTERP_IDLE))
                except Exception:
                    interp = linuxcnc.INTERP_IDLE

                is_tc = bool(getattr(self, "_mdi_is_toolchange", False))
                if not is_tc:
                    if interp != linuxcnc.INTERP_IDLE:
                        return
                    self._mdi_fsm_state = "DONE"
                    return
                else:
                    try:
                        hw_active = bool(self._toolchange_hw_active())
                    except Exception:
                        hw_active = False
                    sent_ts = float(getattr(self, "_mdi_sent_ts", 0.0) or 0.0)
                    if hw_active:
                        return
                    if sent_ts > 0.0 and (time.time() - sent_ts) < 0.20:
                        return
                    self._mdi_fsm_state = "DONE"
                    return

            if st == "DONE":
                try:
                    if self._mdi_fsm_timer is not None:
                        self._mdi_fsm_timer.stop()
                except Exception:
                    pass
                self._mdi_finish(str(getattr(self, "_mdi_last_cmd", "")), True, "")
                return

        except Exception as e:
            try:
                if hasattr(self.cmd, "abort"):
                    self.cmd.abort()
            except Exception:
                pass
            try:
                if self._mdi_fsm_timer is not None:
                    self._mdi_fsm_timer.stop()
            except Exception:
                pass
            self._mdi_finish(str(getattr(self, "_mdi_last_cmd", "")), False, str(e))

    def _mdi_finish(self, cmd_text: str, ok: bool, err: str):
        try:
            if not ok:
                self._append_mdi_history(f"ERR: {err}")
            try:
                s = str(cmd_text).strip().upper()
                m = re.search(r"\bT\s*([0-9]{1,2})\b.*\bM\s*6\b", s)
                if m and ok:
                    tn = int(m.group(1))
                    self._tool_active_virtual = tn
                    self._tool_active_last = None
                    self._update_active_tool_label(tn)
                    self._mdi_pending_tool = 0
                else:
                    m2 = re.fullmatch(r"\s*T\s*([0-9]{1,2})\s*", s)
                    if m2 and ok:
                        self._mdi_pending_tool = int(m2.group(1))
                    m3 = re.fullmatch(r"\s*M\s*6\s*", s)
                    if m3 and ok and int(getattr(self, "_mdi_pending_tool", 0) or 0) > 0:
                        tn = int(self._mdi_pending_tool)
                        self._tool_active_virtual = tn
                        self._tool_active_last = None
                        self._update_active_tool_label(tn)
                        self._mdi_pending_tool = 0
            except Exception:
                pass
        finally:
            try:
                if bool(getattr(self, "_mdi_is_toolchange", False)):
                    prev = int(getattr(self, "_mdi_prev_mode", linuxcnc.MODE_MANUAL))
                    try:
                        self.cmd.mode(prev)
                    except Exception:
                        pass
            except Exception:
                pass
            self._mdi_busy = False

    # ══════════════════════════════════════════════════════════
    # HOMING
    # ══════════════════════════════════════════════════════════

    def _dbg(self, msg: str):
        self._dbg_last_status = msg
        print(f"[ICEQ] {msg}")

    def _wait_for_homed(self, joints, timeout_s: float) -> bool:
        t0 = time.time()
        while (time.time() - t0) < timeout_s:
            try:
                self.stat.poll()
                if all(bool(self.stat.homed[j]) for j in joints if j < len(self.stat.homed)):
                    return True
            except Exception:
                pass
            time.sleep(0.05)
        return False

    def _start_homing_thread(self, joints, timeout_s: float = 90.0):
        if getattr(self, "_homing_busy", False):
            self._dbg("Homing já em andamento; ignorando.")
            return
        self._homing_busy = True
        self._homing_thread = threading.Thread(
            target=self._homing_worker,
            args=(list(joints), float(timeout_s)),
            daemon=True
        )
        self._homing_thread.start()

    def _homing_worker(self, joints, timeout_s: float):
        try:
            self._dbg(f"HOME start joints={joints}")
            self.cmd.mode(linuxcnc.MODE_MANUAL)
            self.cmd.wait_complete()
            for j in joints:
                self._dbg(f"HOME joint {j}: disparando")
                self.cmd.home(j)
                self.cmd.wait_complete()
                ok = self._wait_for_homed([j], timeout_s=timeout_s)
                if not ok:
                    self._dbg(f"HOME joint {j}: TIMEOUT")
                    return
                self._dbg(f"HOME joint {j}: ok")
            self._dbg("HOME concluído")
        except Exception as e:
            self._dbg(f"Erro no HOMING: {e}")
        finally:
            self._homing_busy = False

    # [FIX-1] Joints corretos: X=0, Z=1
    def ref_all(self):
        try:
            self._dbg("HOME ALL (X=joint0, Z=joint1)")
            self._start_homing_thread([0, 1], timeout_s=90.0)
        except Exception as e:
            self._dbg(f"Erro HOME ALL: {e}")

    def ref_x(self):
        try:
            self._dbg("HOME X (joint 0)")
            self._start_homing_thread([0], timeout_s=60.0)
        except Exception as e:
            self._dbg(f"Erro HOME X: {e}")

    def ref_y(self):
        pass  # HAS_Y_AXIS = False

    def ref_z(self):
        try:
            self._dbg("HOME Z (joint 1)")
            self._start_homing_thread([1], timeout_s=60.0)
        except Exception as e:
            self._dbg(f"Erro HOME Z: {e}")

    def zero_g54(self):
        try:
            self.cmd.mode(linuxcnc.MODE_MDI)
            self.cmd.wait_complete()
            self.cmd.mdi("G10 L20 P1 X0 Z0")
            self.cmd.wait_complete()
        except Exception as e:
            print(f"[ICEQ] Erro em zero_g54: {e}")

    # ══════════════════════════════════════════════════════════
    # SPINDLE
    # ══════════════════════════════════════════════════════════

    def _spindle_apply(self):
        try:
            rpm = int(abs(self._spindle_rpm_setpoint))
            if self._spindle_dir == 0 or rpm <= 0:
                self.cmd.spindle(0)
                return
            self.cmd.spindle(int(self._spindle_dir), float(rpm))
        except Exception as e:
            print(f"[ICEQ] spindle_apply erro: {e}")

    def spindle_cw(self):
        try:
            if self._spindle_rpm_setpoint <= 0:
                self._spindle_rpm_setpoint = 100
            self.cmd.spindle(linuxcnc.SPINDLE_FORWARD, self._spindle_rpm_setpoint)
            self._spindle_dir = 1
            self._spindle_running = True
        except Exception as e:
            print(f"[ICEQ] spindle_cw erro: {e}")

    def spindle_ccw(self):
        try:
            if self._spindle_rpm_setpoint <= 0:
                self._spindle_rpm_setpoint = 100
            self.cmd.spindle(linuxcnc.SPINDLE_REVERSE, self._spindle_rpm_setpoint)
            self._spindle_dir = -1
            self._spindle_running = True
        except Exception as e:
            print(f"[ICEQ] spindle_ccw erro: {e}")

    def spindle_stop(self):
        try:
            self.cmd.spindle(linuxcnc.SPINDLE_OFF)
            self._spindle_dir = 0
            self._spindle_running = False
            self._spindle_rpm_setpoint = 0
        except Exception as e:
            print(f"[ICEQ] spindle_stop erro: {e}")

    def spindle_rpm_plus(self):
        try:
            self._spindle_rpm_setpoint = int(self._spindle_rpm_setpoint) + int(self._spindle_step)
            if self._spindle_rpm_setpoint < 0:
                self._spindle_rpm_setpoint = 0
            # Limite máximo lido do INI (MAX_OUTPUT), fallback 1100
            rpm_max = int(getattr(self, "_spindle_rpm_max_val", 0) or 1100)
            if rpm_max <= 0:
                rpm_max = 1100
            if self._spindle_rpm_setpoint > rpm_max:
                self._spindle_rpm_setpoint = rpm_max
            if self._spindle_dir != 0:
                self._spindle_apply()
        except Exception as e:
            print(f"[ICEQ] spindle_rpm_plus erro: {e}")

    def spindle_rpm_minus(self):
        try:
            new_rpm = int(self._spindle_rpm_setpoint) - int(self._spindle_step)
            if new_rpm <= 0:
                self.spindle_stop()
                return
            self._spindle_rpm_setpoint = new_rpm
            if self._spindle_dir != 0:
                self._spindle_apply()
        except Exception as e:
            print(f"[ICEQ] spindle_rpm_minus erro: {e}")

    def _update_spindle_rpm_label(self):
        try:
            rpm_disp = int(abs(self._spindle_rpm_setpoint))
            self._set_label_if_exists("lbl_spindle_rpm", f"{rpm_disp:d} RPM")
        except Exception:
            pass

    def coolant_toggle(self):
        try:
            if not self._coolant_on:
                self.cmd.mist(1)
                self._coolant_on = True
            else:
                self.cmd.mist(0)
                self._coolant_on = False
        except Exception as e:
            print(f"[ICEQ] coolant_toggle erro: {e}")

    def _get_coolant_on_safe(self):
        try:
            mist = bool(getattr(self.stat, "mist", False))
            flood = bool(getattr(self.stat, "flood", False))
            return (mist or flood)
        except Exception:
            return False

    def _get_spindle_rpm_safe(self):
        try:
            for pin in ("spindle.0.speed-in", "motion.spindle-speed-in"):
                try:
                    v = hal.get_value(pin)
                    if v is not None:
                        rpm = abs(float(v))
                        if rpm > 0.1:
                            return rpm
                except Exception:
                    pass
        except Exception:
            pass
        try:
            rpm_base = float(self.stat.spindle[0]['speed'])
            ovr = max(0, min(120, int(getattr(self, "_spindle_ovr_pct", 100))))
            return abs(rpm_base) * (float(ovr) / 100.0)
        except Exception:
            return 0.0

    # ══════════════════════════════════════════════════════════
    # BARRA RPM — [FIX-4/5] usa lbl_rpm_title
    # ══════════════════════════════════════════════════════════

    def _init_spindle_rpm_bar(self):
        from PyQt5.QtWidgets import QProgressBar
        from PyQt5.QtCore import Qt

        if not hasattr(self, "lbl_spindle_rpm"):
            return

        # [FIX-4] nome correto: lbl_rpm_title (sem duplo 't')
        title_lbl = getattr(self, "lbl_rpm_title", None)
        value_lbl = getattr(self, "lbl_spindle_rpm", None)
        if value_lbl is None:
            return

        parent = value_lbl.parentWidget()
        if parent is None:
            return

        if getattr(self, "_spindle_rpm_bar", None) is not None:
            return

        bar = QProgressBar(parent)
        bar.setObjectName("bar_spindle_rpm")
        bar.setOrientation(Qt.Vertical)
        bar.setTextVisible(False)
        bar.setMinimum(0)
        bar.setMaximum(1200)
        bar.setValue(0)

        try:
            bar.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        except Exception:
            pass

        bar.setStyleSheet("""
            QProgressBar#bar_spindle_rpm {
                border: 1px solid #7a7a7a;
                border-radius: 6px;
                background: #efefef;
            }
            QProgressBar#bar_spindle_rpm::chunk {
                border-radius: 5px;
                background: qlineargradient(
                    x1:0, y1:1, x2:0, y2:0,
                    stop:0 #2ecc71,
                    stop:1 #f1c40f
                );
            }
        """)

        self._spindle_rpm_bar = bar
        self._spindle_rpm_bar_relayout()

    def _spindle_rpm_bar_relayout(self):
        try:
            bar = getattr(self, "_spindle_rpm_bar", None)
            if bar is None:
                return

            # [FIX-5] usa lbl_rpm_title
            title_lbl = getattr(self, "lbl_rpm_title", None)
            value_lbl = getattr(self, "lbl_spindle_rpm", None)
            if value_lbl is None:
                return

            rect = value_lbl.geometry()
            if title_lbl is not None:
                rect = rect.united(title_lbl.geometry())

            w = max(int(rect.width()), 140)
            h = max(int(rect.height()), 120)
            rect.setWidth(w)
            rect.setHeight(h)

            Y_OFFSET = -60
            rect.translate(0, Y_OFFSET)

            m = 2
            bar.setGeometry(rect.adjusted(m, m, -m, -m))

            try:
                bar.lower()
            except Exception:
                pass
            try:
                if title_lbl is not None:
                    title_lbl.raise_()
                value_lbl.raise_()
            except Exception:
                pass
            try:
                if title_lbl is not None:
                    st = str(title_lbl.styleSheet() or "")
                    if "background" not in st.lower():
                        title_lbl.setStyleSheet((st + "; background: transparent;").strip("; "))
                st = str(value_lbl.styleSheet() or "")
                if "background" not in st.lower():
                    value_lbl.setStyleSheet((st + "; background: transparent;").strip("; "))
            except Exception:
                pass
        except Exception:
            pass

    def _update_spindle_rpm_bar(self, rpm_value):
        try:
            bar = getattr(self, "_spindle_rpm_bar", None)
            if bar is None:
                return
            self._spindle_rpm_bar_relayout()
            max_rpm = int(getattr(self, "_spindle_rpm_max_val", 0) or 0)
            if max_rpm <= 0:
                max_rpm = int(bar.maximum() or 1200)
            if bar.maximum() != max_rpm:
                bar.setMaximum(max_rpm)
            v = max(0.0, min(float(max_rpm), abs(float(rpm_value or 0.0))))
            iv = int(round(v))
            if bar.value() != iv:
                bar.setValue(iv)
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════
    # HAL helpers
    # ══════════════════════════════════════════════════════════

    def _hal_bit(self, pin_name: str):
        try:
            v = hal.get_value(pin_name)
            return bool(v)
        except Exception:
            pass
        try:
            import subprocess
            out = subprocess.check_output(
                ["halcmd", "getp", pin_name],
                stderr=subprocess.STDOUT, text=True
            ).strip()
            u = out.upper()
            if u in ("TRUE", "1"):
                return True
            if u in ("FALSE", "0"):
                return False
            try:
                return float(out) != 0.0
            except Exception:
                return None
        except Exception:
            return None

    def _hal_bit_multi(self, pin_names):
        for p in pin_names:
            v = self._hal_bit(p)
            if v is not None:
                return v
        return None

    def _hal_float(self, pin_name: str):
        try:
            v = hal.get_value(pin_name)
            return float(v)
        except Exception:
            return None

    def _hal_sets_signal(self, signal_name, value):
        try:
            import subprocess
            if isinstance(value, bool):
                vtxt = "1" if value else "0"
            elif isinstance(value, int):
                vtxt = str(int(value))
            else:
                try:
                    vtxt = str(float(value))
                except Exception:
                    vtxt = str(value)
            r = subprocess.run(
                ["halcmd", "sets", str(signal_name), vtxt],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            if r.returncode != 0:
                err = (r.stderr or r.stdout or "").strip()
                return False, err
            return True, ""
        except Exception as e:
            return False, str(e)

    # ══════════════════════════════════════════════════════════
    # HAL JOG component (HALUI)
    # ══════════════════════════════════════════════════════════

    def _init_hal_jog_component(self):
        try:
            c = hal.component("iceqjog")
            c.newpin("jog-x-pos", hal.HAL_BIT, hal.HAL_OUT)
            c.newpin("jog-x-neg", hal.HAL_BIT, hal.HAL_OUT)
            c.newpin("jog-z-pos", hal.HAL_BIT, hal.HAL_OUT)
            c.newpin("jog-z-neg", hal.HAL_BIT, hal.HAL_OUT)
            c.newpin("jog-speed", hal.HAL_FLOAT, hal.HAL_OUT)
            c["jog-x-pos"] = 0
            c["jog-x-neg"] = 0
            c["jog-z-pos"] = 0
            c["jog-z-neg"] = 0
            c["jog-speed"] = 0.0
            c.ready()
            self._hal_jog_comp = c
            self._hal_jog_ready = True
            print("[ICEQ][JOG] HAL component 'iceqjog' criado")
        except Exception as e:
            self._hal_jog_comp = None
            self._hal_jog_ready = False
            print(f"[ICEQ][JOG] Falha criando iceqjog: {e}")

    def _hal_jog_set(self, pin_name, value):
        try:
            if not bool(getattr(self, "_hal_jog_ready", False)):
                return False
            c = getattr(self, "_hal_jog_comp", None)
            if c is None:
                return False
            c[pin_name] = value
            return True
        except Exception as e:
            print(f"[ICEQ][JOG] erro set iceqjog.{pin_name}={value}: {e}")
            return False

    def _hal_jog_all_off(self):
        self._hal_jog_set("jog-x-pos", 0)
        self._hal_jog_set("jog-x-neg", 0)
        self._hal_jog_set("jog-z-pos", 0)
        self._hal_jog_set("jog-z-neg", 0)

    # ══════════════════════════════════════════════════════════
    # JOG
    # ══════════════════════════════════════════════════════════

    def _jog_machine_ready(self) -> tuple:
        try:
            self.stat.poll()
        except Exception:
            return False, "sem status"
        try:
            if bool(getattr(self.stat, "estop", 0)):
                return False, "E-STOP ativo"
        except Exception:
            pass
        try:
            if not bool(getattr(self.stat, "enabled", 0)):
                return False, "máquina OFF"
        except Exception:
            pass
        try:
            mode = int(getattr(self.stat, "task_mode", -1))
            interp = int(getattr(self.stat, "interp_state", -1))
            if mode == int(linuxcnc.MODE_AUTO) and interp != int(linuxcnc.INTERP_IDLE):
                return False, "programa em execução (AUTO)"
        except Exception:
            pass
        # Home não é obrigatório para JOG (NO_FORCE_HOMING = 1 no INI)
        if bool(getattr(self, "_jog_busy", False)):
            return False, "JOG ocupado"
        return True, ""

    def _is_jog_incremental_mode(self) -> bool:
        try:
            if hasattr(self, "btn_jog_mode"):
                t = str(self.btn_jog_mode.text()).strip().lower()
            elif hasattr(self, "cb_jog_mode"):
                t = str(self.cb_jog_mode.currentText()).strip().lower()
            else:
                return True
            return ("increment" in t)
        except Exception:
            return False

    def _is_jog_continuous_mode(self) -> bool:
        try:
            if hasattr(self, "btn_jog_mode"):
                t = str(self.btn_jog_mode.text()).strip().lower()
            elif hasattr(self, "cb_jog_mode"):
                t = str(self.cb_jog_mode.currentText()).strip().lower()
            else:
                return False
            return ("cont" in t)
        except Exception:
            return False

    def _on_jog_mode_changed(self, _text: str):
        ena = True
        for bn in ("btn_jog_x_plus", "btn_jog_x_minus", "btn_jog_z_plus", "btn_jog_z_minus"):
            try:
                b = getattr(self, bn, None)
                if b is None:
                    continue
                b.setEnabled(ena)
                b.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, not ena)
            except Exception:
                pass

    def _on_btn_jog_mode_clicked(self):
        try:
            opts = list(getattr(self, "_jog_mode_options", ["Contínuo", "Incremental"]))
            idx = int(getattr(self, "_jog_mode_idx", 0))
            idx = (idx + 1) % len(opts)
            self._jog_mode_idx = idx
            txt = str(opts[idx])
            try:
                self.btn_jog_mode.setText(txt)
            except Exception:
                pass
            self._on_jog_mode_changed(txt)
        except Exception as e:
            print(f"[ICEQ][JOG] erro alternando modo: {e}")

    def _on_btn_jog_step_clicked(self):
        try:
            opts = list(getattr(self, "_jog_step_options", [10.0, 1.0, 0.5, 0.1, 0.01, 0.001]))
            idx = int(getattr(self, "_jog_step_idx", 0))
            idx = (idx + 1) % len(opts)
            self._jog_step_idx = idx
            v = float(opts[idx])
            try:
                self.btn_jog_step.setText(f"{v:g} mm")
            except Exception:
                pass
            self._on_jog_step_changed(f"{v:g}")
        except Exception as e:
            print(f"[ICEQ][JOG] erro alternando passo: {e}")

    def _jog_cont_signal_names(self, axis):
        a = str(axis).strip().upper()
        if not hasattr(self, "_jog_cont_signal_map"):
            self._jog_cont_signal_map = {
                "X": ("axis-select-x", "axis-select-z"),
                "Z": ("axis-select-z", "axis-select-x"),
            }
        if a not in self._jog_cont_signal_map:
            raise ValueError(f"Axis inválido para JOG contínuo: {a}")
        return self._jog_cont_signal_map[a]

    def _jog_continuous_press(self, axis, direction):
        try:
            a = str(axis).strip().upper()
            d = 1 if int(direction) >= 1 else -1

            joint_idx = self._JOINT_MAP.get(a)
            if joint_idx is None:
                return

            vcur = float(getattr(self, "_jog_speed_current_mm_min", 0.0) or 0.0)
            if vcur <= 0.0:
                vcur = float(getattr(self, "_jog_speed_max_mm_min", 1000.0) or 1000.0)

            self._jog_cont_active = True
            self._jog_cont_axis = a

            def _do_jog():
                try:
                    try:
                        lu = float(getattr(self.stat, "linear_units", 1.0))
                    except Exception:
                        lu = 1.0
                    speed_units_s = (vcur / 60.0)
                    if lu < 0.999:
                        speed_units_s *= lu
                    elif lu > 1.001:
                        speed_units_s /= lu
                    self.cmd.mode(linuxcnc.MODE_MANUAL)
                    self.cmd.jog(linuxcnc.JOG_CONTINUOUS, 0, joint_idx, d * speed_units_s)
                except Exception as e:
                    print(f"[ICEQ][JOG] contínuo thread erro: {e}")

            threading.Thread(target=_do_jog, daemon=True).start()

        except Exception as e:
            print(f"[ICEQ][JOG] contínuo erro ao iniciar ({axis}): {e}")

    def _jog_continuous_release(self, axis):
        try:
            a = str(axis).strip().upper()
            joint_idx = self._JOINT_MAP.get(a)
            if joint_idx is None:
                return
            self._jog_cont_active = False
            self._jog_cont_axis = ""

            def _do_stop():
                try:
                    self.cmd.jog(linuxcnc.JOG_STOP, 0, joint_idx)
                except Exception as e:
                    print(f"[ICEQ][JOG] contínuo stop erro: {e}")

            threading.Thread(target=_do_stop, daemon=True).start()
        except Exception as e:
            print(f"[ICEQ][JOG] contínuo erro ao parar ({axis}): {e}")

    def keyPressEvent(self, event):
        """Setas do teclado fazem JOG contínuo — igual ao Axis."""
        try:
            key = event.key()
            if event.isAutoRepeat():
                return
            mapping = {
                QtCore.Qt.Key_Left:  ("Z", -1),
                QtCore.Qt.Key_Right: ("Z", +1),
                QtCore.Qt.Key_Up:    ("X", -1),
                QtCore.Qt.Key_Down:  ("X", +1),
            }
            if key in mapping:
                axis, direction = mapping[key]
                self._jog_continuous_press(axis, direction)
                return
        except Exception:
            pass
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        """Soltar seta para o JOG."""
        try:
            key = event.key()
            if event.isAutoRepeat():
                return
            mapping = {
                QtCore.Qt.Key_Left:  "Z",
                QtCore.Qt.Key_Right: "Z",
                QtCore.Qt.Key_Up:    "X",
                QtCore.Qt.Key_Down:  "X",
            }
            if key in mapping:
                axis = mapping[key]
                self._jog_continuous_release(axis)
                return
        except Exception:
            pass
        super().keyReleaseEvent(event)

    def _jog_click(self, axis_letter: str, direction: int):
        """
        JOG incremental por JOINT.
        [FIX-1] Usa self._JOINT_MAP para mapeamento correto X=0, Z=1
        """
        try:
            axis = str(axis_letter).upper().strip()
            if axis not in ("X", "Z"):
                return

            if not self._is_jog_incremental_mode():
                if self._is_jog_continuous_mode():
                    return
                print("[ICEQ][JOG] bloqueado: modo CONTÍNUO")
                return

            ok, reason = self._jog_machine_ready()
            if not ok:
                print(f"[ICEQ][JOG] bloqueado: {reason}")
                return

            step_mm = float(getattr(self, "_jog_step_mm", 0.1))
            step_mm *= (1.0 if int(direction) >= 0 else -1.0)

            # [FIX-1] usa _JOINT_MAP correto
            joint_idx = self._JOINT_MAP.get(axis)
            if joint_idx is None:
                print(f"[ICEQ][JOG] eixo {axis} não mapeado")
                return

            try:
                self.stat.poll()
                joints = list(getattr(self.stat, "joint_position", []))
            except Exception:
                print("[ICEQ][JOG] falha ao ler joint_position")
                return

            if joint_idx >= len(joints):
                print("[ICEQ][JOG] joint fora do range")
                return

            # Valida soft-limits
            try:
                joints_cfg = list(getattr(self.stat, "joint", []))
                if joint_idx < len(joints_cfg):
                    cur_pos = float(joints[joint_idx])
                    try:
                        lu = float(getattr(self.stat, "linear_units", 1.0))
                    except Exception:
                        lu = 1.0

                    def mm_to_machine(v_mm):
                        if lu < 0.999:
                            return v_mm * lu
                        elif lu > 1.001:
                            return v_mm / lu
                        return v_mm

                    delta_mu = mm_to_machine(step_mm)
                    target_mu = cur_pos + delta_mu
                    lo = float(getattr(joints_cfg[joint_idx], "min_position_limit", -1e9))
                    hi = float(getattr(joints_cfg[joint_idx], "max_position_limit",  1e9))
                    if not (lo <= target_mu <= hi):
                        print(f"[ICEQ][JOG] bloqueado por limite joint {joint_idx}")
                        return
            except Exception:
                pass

            self._jog_busy = True
            self._jog_finish_axis = axis
            self._jog_finish_deadline = time.time() + 10.0
            if not self._jog_finish_timer.isActive():
                self._jog_finish_timer.start(60)

            t = threading.Thread(
                target=self._jog_worker_mdi_increment,
                args=(axis, float(step_mm)),
                daemon=True
            )
            t.start()

        except Exception as e:
            self._jog_busy = False
            print(f"[ICEQ][JOG] erro inesperado: {e}")

    def _jog_worker_mdi_increment(self, axis: str, dist_mm: float):
        try:
            try:
                self.stat.poll()
                lu = float(getattr(self.stat, "linear_units", 1.0))
            except Exception:
                lu = 1.0

            def mm_to_machine(v_mm: float) -> float:
                try:
                    v_mm = float(v_mm)
                    if lu < 0.999:
                        return v_mm * lu
                    elif lu > 1.001:
                        return v_mm / lu
                    return v_mm
                except Exception:
                    return float(v_mm)

            dist_mu = mm_to_machine(dist_mm)

            try:
                vmax_mm_min = float(getattr(self, "_jog_speed_max_mm_min", 1000.0))
                pct = int(getattr(self, "_jog_speed_pct", 100))
                pct = max(0, min(120, pct))
                vcur_mm_min = vmax_mm_min * (float(pct) / 100.0)
            except Exception:
                vcur_mm_min = 1000.0

            try:
                feed_mu_min = mm_to_machine(vcur_mm_min)
            except Exception:
                feed_mu_min = vcur_mm_min

            if float(feed_mu_min) < 1.0:
                feed_mu_min = 1.0

            cmd_move_inc = f"G91 G1 {axis}{dist_mu:.5f} F{float(feed_mu_min):.1f}"
            cmd_abs = "G90"
            cmd_restore_rapid = "G0"

            print(f"[ICEQ][JOG] {axis} click -> {cmd_move_inc}")

            with self._cmd_lock:
                try:
                    self.cmd.mode(linuxcnc.MODE_MDI)
                except Exception:
                    pass
                self.cmd.mdi(cmd_move_inc)
                self.cmd.mdi(cmd_abs)
                self.cmd.mdi(cmd_restore_rapid)

        except Exception as e:
            print(f"[ICEQ][JOG] exceção worker: {e}")

    def _jog_finish_tick(self):
        if not bool(getattr(self, "_jog_busy", False)):
            try:
                if getattr(self, "_jog_finish_timer", None) is not None:
                    self._jog_finish_timer.stop()
            except Exception:
                pass
            return

        try:
            self.stat.poll()
        except Exception:
            pass

        try:
            interp_idle = int(getattr(linuxcnc, "INTERP_IDLE", 1))
            interp_state = int(getattr(self.stat, "interp_state", interp_idle))
            is_idle = (interp_state == interp_idle)
        except Exception:
            is_idle = True

        try:
            if time.time() > float(getattr(self, "_jog_finish_deadline", 0.0) or 0.0):
                is_idle = True
        except Exception:
            pass

        if not is_idle:
            return

        try:
            with self._cmd_lock:
                self.cmd.mode(linuxcnc.MODE_MDI)
                self.cmd.mdi("G90")
        except Exception:
            pass

        try:
            prev = int(getattr(self, "_jog_prev_mode", linuxcnc.MODE_MANUAL))
            back = prev if prev in (linuxcnc.MODE_MANUAL, linuxcnc.MODE_AUTO) else linuxcnc.MODE_MANUAL
            try:
                self.cmd.mode(back)
            except Exception:
                pass
        except Exception:
            pass

        self._jog_busy = False
        try:
            if getattr(self, "_jog_finish_timer", None) is not None:
                self._jog_finish_timer.stop()
        except Exception:
            pass

    def _on_jog_step_changed(self, text: str):
        try:
            if not text:
                return
            txt = str(text).strip().lower().replace(",", ".")
            m = re.search(r"([-+]?\d*\.?\d+)", txt)
            if not m:
                return
            value = float(m.group(1))
            if value <= 0.0:
                return
            self._jog_step_mm = value
        except Exception as e:
            print(f"[ICEQ][JOG] erro ao ler passo: {e}")

    # ══════════════════════════════════════════════════════════
    # JOG VELOCIDADE
    # ══════════════════════════════════════════════════════════

    def _get_jog_max_mm_min_safe(self) -> float:
        try:
            import configparser
            ini_path = (
                os.environ.get("INI_FILE_NAME")
                or os.environ.get("EMC2_INI_FILE_NAME")
                or ""
            )
            if not ini_path:
                return 1000.0

            cfg = configparser.ConfigParser()
            cfg.read(ini_path)

            def _getf(section, key, default=None):
                try:
                    v = cfg.get(section, key, fallback=None)
                    if v is None:
                        return default
                    return float(str(v).strip())
                except Exception:
                    return default

            vx = _getf("AXIS_X", "MAX_VELOCITY", None)
            vz = _getf("AXIS_Z", "MAX_VELOCITY", None)
            vt = _getf("TRAJ", "MAX_LINEAR_VELOCITY", None)
            if vt is None:
                vt = _getf("TRAJ", "MAX_VELOCITY", None)

            candidates = [v for v in (vx, vz, vt) if isinstance(v, (int, float)) and v > 0.0]
            if not candidates:
                return 1000.0

            v_units_per_s = max(candidates)

            try:
                self.stat.poll()
                lu = float(getattr(self.stat, "linear_units", 1.0))
            except Exception:
                lu = 1.0

            def to_mm(v):
                if lu < 0.999:
                    return v / lu
                elif lu > 1.001:
                    return v * lu
                return v

            v_mm_min = to_mm(v_units_per_s) * 60.0
            return float(v_mm_min) if v_mm_min > 1.0 else 1000.0

        except Exception:
            return 1000.0

    def _get_traj_max_mm_min_safe(self) -> float:
        try:
            import configparser
            ini_path = (
                os.environ.get("INI_FILE_NAME")
                or os.environ.get("EMC2_INI_FILE_NAME")
                or ""
            )
            if not ini_path:
                return 0.0

            cfg = configparser.RawConfigParser(inline_comment_prefixes=(";", "#"), strict=False)
            if not cfg.read(ini_path):
                return 0.0

            def _parse_ini_number(raw):
                if raw is None:
                    return None
                s = str(raw).strip().split(";")[0].split("#")[0].strip()
                m = re.search(r"[-+]?\d+(?:[.,]\d+)?", s)
                if not m:
                    return None
                try:
                    return float(m.group(0).replace(",", "."))
                except Exception:
                    return None

            def _getf(section, key):
                try:
                    if not cfg.has_section(section):
                        return None
                    return _parse_ini_number(cfg.get(section, key, fallback=None))
                except Exception:
                    return None

            units = "mm"
            try:
                units = str(cfg.get("TRAJ", "LINEAR_UNITS", fallback="mm")).strip().lower()
            except Exception:
                pass
            unit_to_mm = 25.4 if units.startswith("in") else 1.0

            v_traj = _getf("TRAJ", "MAX_LINEAR_VELOCITY") or _getf("TRAJ", "MAX_VELOCITY")
            vx = _getf("AXIS_X", "MAX_VELOCITY")
            vz = _getf("AXIS_Z", "MAX_VELOCITY")
            vj0 = _getf("JOINT_0", "MAX_VELOCITY")
            vj1 = _getf("JOINT_1", "MAX_VELOCITY")

            axis_candidates = [v for v in (vx, vz, vj0, vj1) if v and v > 0.0]
            v_axis = max(axis_candidates) if axis_candidates else None

            candidates_final = []
            if v_traj and v_traj > 0.0:
                candidates_final.append(v_traj)
            if v_axis and v_axis > 0.0:
                candidates_final.append(v_axis)

            if not candidates_final:
                return 0.0

            if v_traj and v_traj > 0.0 and v_axis and v_axis > 0.0:
                v_units_per_s = min(v_traj, v_axis)
            else:
                v_units_per_s = max(candidates_final)

            v_mm_min = (v_units_per_s * unit_to_mm) * 60.0
            return float(v_mm_min) if v_mm_min > 1.0 else 0.0
        except Exception:
            return 0.0

    def _sync_jog_speed_widgets(self, pct: int):
        pct_i = int(max(0, min(120, int(pct))))
        if hasattr(self, "sld_vel_jog_oper") and self.sld_vel_jog_oper.value() != pct_i:
            self.sld_vel_jog_oper.blockSignals(True)
            self.sld_vel_jog_oper.setValue(pct_i)
            self.sld_vel_jog_oper.blockSignals(False)
        if hasattr(self, "spn_jog_speed") and self.spn_jog_speed.value() != pct_i:
            self.spn_jog_speed.blockSignals(True)
            self.spn_jog_speed.setValue(pct_i)
            self.spn_jog_speed.blockSignals(False)

    def _update_jog_speed_title(self):
        try:
            vmax = float(getattr(self, "_jog_speed_max_mm_min", 1000.0))
            pct = int(getattr(self, "_jog_speed_pct", 100))
            vcur = vmax * (float(pct) / 100.0)
            if hasattr(self, "lbl_jog_value"):
                self.lbl_jog_value.setText(f"{vcur:.0f} mm/min")
                return
            if hasattr(self, "lbl_jog_tittle"):
                self.lbl_jog_tittle.setText(f"VELOCIDADE JOG: {vcur:.0f} mm/min")
        except Exception:
            pass

    def _apply_jog_speed_pct(self, pct: int):
        pct_i = int(max(0, min(120, int(pct))))
        self._jog_speed_pct = pct_i
        self._update_jog_speed_title()
        self._sync_jog_speed_widgets(pct_i)
        try:
            vmax = float(getattr(self, "_jog_speed_max_mm_min", 1000.0))
            vcur = vmax * (float(pct_i) / 100.0)
            self._jog_speed_current_mm_min = vcur
        except Exception:
            pass

    def _on_jog_speed_slider_changed(self, value: int):
        try:
            self._apply_jog_speed_pct(int(value))
        except Exception as e:
            print(f"[ICEQ][JOG] erro slider jog: {e}")

    def _on_jog_speed_spin_changed(self, value: int):
        try:
            self._apply_jog_speed_pct(int(value))
        except Exception as e:
            print(f"[ICEQ][JOG] erro spin jog: {e}")

    # ══════════════════════════════════════════════════════════
    # CLOUD helpers
    # ══════════════════════════════════════════════════════════

    def _cloud_now_ts(self) -> float:
        try:
            return float(time.time())
        except Exception:
            return 0.0

    def _cloud_rate_ok(self, key: str) -> bool:
        try:
            t = self._cloud_now_ts()
            last = float(getattr(self, "_cloud_last_sent_ts", {}).get(key, 0.0) or 0.0)
            if (t - last) < float(getattr(self, "_cloud_min_interval_s", 1.0) or 1.0):
                return False
            self._cloud_last_sent_ts[key] = t
            return True
        except Exception:
            return True

    def _cloud_send_transition_log(self, event: str, state: dict, prev: dict):
        try:
            if not bool(getattr(self, "_cloud_enabled", False)):
                return
            if not (self.cloud and self.cloud.is_configured()):
                return
        except Exception:
            return

        if not self._cloud_rate_ok(f"tr:{event}"):
            return

        try:
            payload = {"event": str(event), "state": state or {}, "prev": prev or {}, "source": "ihm"}
            self.cloud.send_log(log_type="transition", payload=payload, severity="info")
        except Exception as e:
            print(f"[ICEQ][CLOUD] falha enviando transição: {e}")

    def _cloud_collect_state(self) -> dict:
        st = {}
        try:
            self.stat.poll()
        except Exception:
            pass
        for key, attr in [("estop", "estop"), ("enabled", "enabled"),
                           ("task_mode", "task_mode"), ("interp_state", "interp_state"),
                           ("paused", "paused")]:
            try:
                st[key] = getattr(self.stat, attr, None)
            except Exception:
                st[key] = None
        try:
            sp_on, sp_dir = self._get_spindle_fb()
            st["spindle_on"] = bool(sp_on)
            st["spindle_dir"] = int(sp_dir)
        except Exception:
            st["spindle_on"] = False
            st["spindle_dir"] = 0
        try:
            st["coolant"] = bool(getattr(self, "_coolant_on", False))
        except Exception:
            st["coolant"] = False
        try:
            st["tool"] = int(getattr(self.stat, "tool_in_spindle", 0))
        except Exception:
            st["tool"] = int(getattr(self, "_tool_active_virtual", 0) or 0)
        try:
            st["toolchange_active"] = bool(getattr(self, "_toolchange_busy", False))
        except Exception:
            st["toolchange_active"] = False
        return st

    def _cloud_ping_tick(self):
        try:
            if not bool(getattr(self, "_cloud_enabled", False)):
                return
            if not (self.cloud and self.cloud.is_configured()):
                return
            self.cloud.send_ping()
        except Exception as e:
            print(f"[ICEQ][CLOUD] ping falhou: {e}")

    def _cloud_transition_tick(self):
        try:
            if not bool(getattr(self, "_cloud_enabled", False)):
                return
            cur = self._cloud_collect_state()
            prev = getattr(self, "_cloud_last_state", None)
            if not isinstance(prev, dict):
                self._cloud_last_state = cur
                return
            changed = [k for k in ("estop", "enabled", "task_mode", "interp_state",
                                   "paused", "spindle_on", "spindle_dir",
                                   "coolant", "tool", "toolchange_active")
                       if prev.get(k) != cur.get(k)]
            if not changed:
                return
            if "estop" in changed:
                event = "estop_changed"
            elif "enabled" in changed:
                event = "machine_enabled_changed"
            elif any(k in changed for k in ("task_mode", "interp_state", "paused")):
                event = "program_state_changed"
            elif any(k in changed for k in ("spindle_on", "spindle_dir")):
                event = "spindle_state_changed"
            elif "coolant" in changed:
                event = "coolant_changed"
            elif "toolchange_active" in changed:
                event = "toolchange_activity_changed"
            elif "tool" in changed:
                event = "tool_changed"
            else:
                event = "state_changed"
            cur2 = dict(cur)
            cur2["_changed_keys"] = changed
            self._cloud_send_transition_log(event=event, state=cur2, prev=prev)
            self._cloud_last_state = cur
        except Exception as e:
            print(f"[ICEQ][CLOUD] transition tick falhou: {e}")

    def _get_spindle_fb(self):
        spindle_on_fb = False
        spindle_dir_fb = 0
        try:
            sp = self.stat.spindle[0]
            spindle_on_fb = bool(getattr(sp, "enabled", False))
            spindle_dir_fb = int(getattr(sp, "direction", 0))
            if not spindle_on_fb:
                try:
                    s = float(getattr(sp, "speed", 0.0))
                    spindle_on_fb = abs(s) > 0.0
                except Exception:
                    pass
        except Exception:
            pass
        return spindle_on_fb, spindle_dir_fb

    # ══════════════════════════════════════════════════════════
    # UTILITÁRIOS
    # ══════════════════════════════════════════════════════════

    def _format_hms(self, seconds):
        try:
            seconds = int(max(0, float(seconds)))
        except Exception:
            seconds = 0
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    def _count_gcode_lines(self, text):
        try:
            total = 0
            for ln in text.splitlines():
                s = ln.strip()
                if not s or s.startswith(";") or s.startswith("("):
                    continue
                total += 1
            return max(1, total)
        except Exception:
            return 0

    def _hal_out_from_label(self, label_widget_name: str):
        try:
            if not hasattr(self, label_widget_name):
                return None
            txt = str(getattr(self, label_widget_name).text()).strip()
            if not txt:
                return None
            u = txt.upper()
            if "." in txt:
                return self._hal_bit(txt)
            if u.startswith("OUT"):
                num = int(u.replace("OUT", "").strip())
                return self._hal_bit_multi([
                    f"motion.digital-out-{num:02d}",
                    f"motion.digital-out-{num}",
                ])
        except Exception:
            return None


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
# ADAPTADOR DE RESOLUÇÃO
# O .ui foi projetado em portrait 1080x1983.
# Em monitores menores (ex: 1024x768 landscape), envolve a IHM
# num QScrollArea para que tudo fique acessível sem cortes.
# ─────────────────────────────────────────────────────────────

class IceqScrollWrapper(QtWidgets.QWidget):
    """
    Wrapper que detecta a resolução disponível e, se necessária,
    envolve a IHM num QScrollArea com scroll suave (touch-friendly).
    """

    # Resolução mínima para exibir sem scroll (portrait nativo)
    MIN_W_NATIVE = 1080
    MIN_H_NATIVE = 1920

    def __init__(self):
        super().__init__()

        screen = QtWidgets.QApplication.primaryScreen()
        screen_geom = screen.availableGeometry()
        sw = int(screen_geom.width())
        sh = int(screen_geom.height())
        print(f"[ICEQ] Resolução disponível: {sw}x{sh}")

        self._ihm = IceqMainWindow()

        needs_scroll = (sw < self.MIN_W_NATIVE or sh < self.MIN_H_NATIVE)

        if needs_scroll:
            print(f"[ICEQ] Modo scroll ativado (tela menor que {self.MIN_W_NATIVE}x{self.MIN_H_NATIVE})")

            scroll = QtWidgets.QScrollArea(self)
            scroll.setWidget(self._ihm)
            scroll.setWidgetResizable(False)   # mantém tamanho original do .ui
            scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
            scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)

            # Scroll suave para touch
            try:
                from PyQt5.QtWidgets import QScroller
                QScroller.grabGesture(
                    scroll.viewport(),
                    QScroller.LeftMouseButtonGesture
                )
            except Exception:
                pass

            lay = QtWidgets.QVBoxLayout(self)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.addWidget(scroll)
            self.setLayout(lay)

            # Janela ocupa toda a tela disponível
            self.setGeometry(screen_geom)
            self.setWindowTitle("ICEQ CNC - IHM")
            self.showMaximized()

        else:
            # Resolução suficiente — exibe a IHM diretamente
            lay = QtWidgets.QVBoxLayout(self)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.addWidget(self._ihm)
            self.setLayout(lay)
            self.setWindowTitle("ICEQ CNC - IHM")
            self.showMaximized()

    def closeEvent(self, event):
        try:
            self._ihm.close()
        except Exception:
            pass
        super().closeEvent(event)


if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    wrapper = IceqScrollWrapper()
    sys.exit(app.exec_())
