#!/usr/bin/env python3
import sys
from PyQt5 import QtWidgets, uic, QtCore, QtGui
from PyQt5.QtWidgets import QFileDialog
from iceq_cloud_client import IceqCloudClient
import linuxcnc
import time
import hal
import threading
import re
import math

class IceqPreview2D(QtCore.QObject):
    """
    Preview 2D simples em XZ para LinuxCNC:
    - Interpreta G0/G1/G2/G3 (plano XZ) e desenha caminhos.
    - Estilos: rápido tracejado, corte normal, acabamento em negrito.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self._view = None
        self._scene = None
        self._container = None

        self._last_file = ""
        self._last_mtime = 0.0

        self._segments = []   # dicts com src_line
        self._items = []      # QGraphicsLineItem por segmento (mesma ordem)
        self._bbox = None

        # highlight de linha atual
        self._active_src_line = -1
        self._active_seg_idx = set()


        # marcador posição atual
        self._pos_item = None
        self._pos_radius = 2.5

        # Controle de zoom
        self._zoom_factor_step = 1.20  # 20% por clique
        self._zoom_min = 0.10          # 10% (limite inferior)
        self._zoom_max = 20.0          # 2000% (limite superior)
        self._default_transform = None

        # Configs (ajuste fino)
        self.finish_feed_threshold = 120.0  # mm/min (opcional)
        self.arc_steps_per_rev = 180        # suavidade de arco

        # Estilos
        self._pen_cut = QtGui.QPen(QtGui.QColor(60, 170, 255))
        self._pen_cut.setWidthF(1.6)

        self._pen_finish = QtGui.QPen(QtGui.QColor(255, 210, 80))
        self._pen_finish.setWidthF(3.2)

        self._pen_rapid = QtGui.QPen(QtGui.QColor(170, 170, 170))
        self._pen_rapid.setWidthF(1.4)
        self._pen_rapid.setStyle(QtCore.Qt.DashLine)

        self._pen_axis = QtGui.QPen(QtGui.QColor(90, 90, 90))
        self._pen_axis.setWidthF(1.0)

        # Pen para destacar a linha atual (estilo "linha ativa")
        self._pen_active_cut = QtGui.QPen(QtGui.QColor(255, 80, 80))
        self._pen_active_cut.setWidthF(3.0)

        self._pen_active_finish = QtGui.QPen(QtGui.QColor(255, 80, 80))
        self._pen_active_finish.setWidthF(4.2)

        self._pen_active_rapid = QtGui.QPen(QtGui.QColor(255, 80, 80))
        self._pen_active_rapid.setWidthF(2.6)
        self._pen_active_rapid.setStyle(QtCore.Qt.DashLine)


        self._brush_pos = QtGui.QBrush(QtGui.QColor(255, 70, 70))

    def attach(self, container_widget: QtWidgets.QWidget):
        """
        Encaixa um QGraphicsView dentro do widget do .ui (w_preview_2d).
        """
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

        # Guardar o transform padrão (zoom 100%)
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
        """
        Desenha eixos X e Z (linhas de referência).
        """
        if self._scene is None:
            return

        # eixo Z horizontal (Z=0), eixo X vertical (X=0)
        # desenha numa janela "genérica"; depois o fitInView ajusta com bbox real
        self._scene.addLine(-200, 0, 200, 0, self._pen_axis)  # Z
        self._scene.addLine(0, -200, 0, 200, self._pen_axis)  # X

    def tick_live(self, stat):
        """
        Atualiza marcador da posição atual (X,Z). Pode ser chamado a cada update_status_panel().
        """
        if self._scene is None:
            return

        try:
            # Em torno, normalmente X e Z estão em stat.position ou stat.actual_position.
            # Para robustez, tenta ambos.
            if hasattr(stat, "actual_position"):
                x = float(stat.actual_position[0])
                z = float(stat.actual_position[2])
            else:
                x = float(stat.position[0])
                z = float(stat.position[2])
        except Exception:
            return

        # Observação: no QGraphicsScene, Y cresce para baixo.
        # Vamos mapear: eixo horizontal = Z, eixo vertical = X (invertendo sinal para "cima positivo").
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
        """
        Recarrega o preview se o arquivo mudou.
        """
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
        """
        Faz parse do G-code e desenha o caminho.
        """
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
            x1 = s["x1"]
            z1 = s["z1"]
            x2 = s["x2"]
            z2 = s["z2"]
            stype = s["type"]
            finish = s["finish"]

            # QGraphics: (x,y) -> (Z, -X)
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
        """
        Auto: enquadra todo o caminho (bbox do programa).
        """
        self._fit_view()

    def reset_view(self):
        """
        Reset: volta para zoom 100% e centraliza no zero (Z=0, X=0).
        """
        if self._view is None:
            return

        if self._default_transform is not None:
            self._view.setTransform(self._default_transform)
        else:
            self._view.resetTransform()

        # Centraliza em origem (scene usa: X=Z, Y=-X)
        self._view.centerOn(0.0, 0.0)

    def _apply_zoom(self, factor: float):
        """
        Aplica zoom relativo com limites.
        """
        if self._view is None:
            return

        try:
            # escala atual aproximada (m11 == escala X)
            cur = float(self._view.transform().m11())
        except Exception:
            cur = 1.0

        new_scale = cur * float(factor)

        if new_scale < self._zoom_min:
            factor = self._zoom_min / max(cur, 1e-9)
        elif new_scale > self._zoom_max:
            factor = self._zoom_max / max(cur, 1e-9)

        # Zoom em torno do centro da viewport
        self._view.setTransformationAnchor(QtWidgets.QGraphicsView.AnchorViewCenter)
        self._view.scale(float(factor), float(factor))


    def tick_program_line(self, stat):
        """
        Detecta a linha atual em execução (quando disponível) e aplica highlight
        nos segmentos com src_line correspondente.
        """
        if self._scene is None or not self._segments or not self._items:
            return

        # LinuxCNC costuma expor motion_line em modo AUTO
        line_no = -1
        try:
            line_no = int(getattr(stat, "motion_line", -1))
        except Exception:
            line_no = -1

        # fallback (dependendo do build/config)
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
        """
        Aplica/atualiza highlight para todos segmentos que pertencem à linha src_line.
        """
        if self._scene is None:
            return

        # Remove highlight anterior (restaura pens originais)
        if self._active_seg_idx:
            for i in self._active_seg_idx:
                if 0 <= i < len(self._segments) and 0 <= i < len(self._items):
                    s = self._segments[i]
                    it = self._items[i]
                    it.setPen(self._pen_for_segment(s, active=False))

        # Aplica highlight na nova linha
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

        # active
        if stype == "rapid":
            return self._pen_active_rapid
        return (self._pen_active_finish if finish else self._pen_active_cut)


    # -------------------------
    # Parser G-code (simples)
    # -------------------------

    def _parse_gcode_to_segments(self, lines):
        x = 0.0
        z = 0.0
        feed = None

        # Estado modal
        motion = 0  # 0=G0, 1=G1, 2=G2, 3=G3
        abs_mode = True  # G90 absoluto / G91 incremental

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
            # 1) Comentário explícito
            u = raw_line.upper()
            if "(FINISH)" in u or ";FINISH" in u or " FINISH" in u:
                return True
            # 2) Opcional por feed baixo + G1
            try:
                if local_feed is not None and float(local_feed) > 0.0:
                    return float(local_feed) <= float(self.finish_feed_threshold)
            except Exception:
                pass
            return False

        # remove comentários (mantendo uma cópia para regra FINISH)
        def strip_comments_keep(raw: str):
            raw2 = raw
            # remove ( ... )
            raw2 = re.sub(r"\(.*?\)", " ", raw2)
            # remove ; ...
            raw2 = raw2.split(";")[0]
            return raw2

        # extrai palavras tipo X.. Z.. I.. K.. F.. G..
        word_re = re.compile(r"([A-Z])\s*([+\-]?\d+(\.\d+)?)", re.I)

        for idx0, raw in enumerate(lines):
            src_line = idx0 + 1  # linha no arquivo (1-based)
            raw_line = raw.rstrip("\n")
            line = strip_comments_keep(raw_line).strip().upper()

            if not line:
                continue

            words = dict()
            for m in word_re.finditer(line):
                words[m.group(1).upper()] = float(m.group(2))

            # modos (G90/G91)
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

            # alvo X/Z
            tx = x
            tz = z
            if "X" in words:
                tx = (words["X"] if abs_mode else (x + words["X"]))
            if "Z" in words:
                tz = (words["Z"] if abs_mode else (z + words["Z"]))

            # se não tem movimento efetivo, continua
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
                # Arco no plano XZ: offsets I (X) e K (Z) relativos ao ponto inicial
                # Formato clássico: G2/G3 X.. Z.. I.. K..
                if "I" not in words and "K" not in words:
                    # sem centro -> degrada para linha
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

                # sentido: G2 CW, G3 CCW
                if motion == 2:
                    # CW: ângulo decresce
                    if a1 >= a0:
                        a1 -= 2.0 * math.pi
                    da = a1 - a0
                else:
                    # CCW: ângulo cresce
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



# -------------------------------------------------
# CONFIGURACAO DA MAQUINA
# Defina se esta maquina tem eixo Y ou nao
# -------------------------------------------------
HAS_Y_AXIS = False  # Torno atual = apenas X e Z. Futuro com Y -> mudar para True.

class IceqMainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()

        # --- QT STYLE (Preview: Cleanlooks / Fusion) ---
        try:
            app = QtWidgets.QApplication.instance()
            if app is not None:
                # tenta Cleanlooks; se não existir no sistema, cai para Fusion
                wanted = "Cleanlooks"
                if wanted in QtWidgets.QStyleFactory.keys():
                    app.setStyle(wanted)
                else:
                    app.setStyle("Fusion")
        except Exception:
            # fallback seguro: não quebra a IHM se algo falhar
            pass


        # carrega o arquivo .ui que está na mesma pasta
        uic.loadUi("iceq_torno.ui", self)
        self.preview2d = IceqPreview2D(self)
        self.preview2d.attach(self.w_preview_2d)
        self._preview2d_last_stat_file = ""

        # -----------------------------------------------------
        # UI: Barra vertical de RPM (SPINDLE) — estilo "Axis"
        # Usa o retângulo das labels lbl_rpm_tittle + lbl_spindle_rpm
        # -----------------------------------------------------
        try:
            self._init_spindle_rpm_bar()
        except Exception as e:
            print(f"[ICEQ] init spindle rpm bar erro: {e}")


        # -----------------------------------------------------
        # UI: label RPM máximo (fonte 10)
        # -----------------------------------------------------
        try:
            f = self.lbl_rpm_max.font()
            f.setPointSize(10)
            self.lbl_rpm_max.setFont(f)
        except Exception:
            pass


        # === EDITOR ===
        self._editor_current_file = None
        self.btn_editor_save.clicked.connect(self._editor_save)

        # === PROGRAM ===
        self._program_loaded_path = None


        # --- PREVIEW 2D: botões de navegação (Zoom / Auto / Reset) ---
        try:
            self.btn_preview_zoom_plus.clicked.connect(self.preview2d.zoom_in)
            self.btn_preview_zoom_minus.clicked.connect(self.preview2d.zoom_out)
            self.btn_preview_auto.clicked.connect(self.preview2d.fit_all)
            self.btn_preview_reset.clicked.connect(self.preview2d.reset_view)
        except Exception:
            pass


        # ------------------------------------------------------------
        # HAL component (ICEQ) para JOG CONTÍNUO via HALUI (sem halcmd)
        # ------------------------------------------------------------
        self._hal_jog_comp = None
        self._hal_jog_ready = False
        self._hal_jog_pins = {}

        self._init_hal_jog_component()


        # ----- objetos do LinuxCNC -----
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


        # setpoint do spindle (S do gcode / MDI) para calculo/label quando necessario
        self._spindle_rpm_setpoint = 0.0

        # ----------------- progresso do programa (barra) -----------------
        self._gcode_total_lines = 0
        self._last_progress_pct = 0

        # garante range/visual do progressbar (se existir)
        if hasattr(self, "prg_cycle_top"):
            self.prg_cycle_top.setRange(0, 100)
            self.prg_cycle_top.setValue(0)
            self.prg_cycle_top.setTextVisible(True)
            self.prg_cycle_top.setFormat("0%")

        # ----------------------------------------------------
        # CICLO: tempo + progresso (barra superior)
        # ----------------------------------------------------
        self._cycle_running = False
        self._cycle_start_ts = None
        self._cycle_last_elapsed = 0.0

        self._gcode_total_lines = 0
        self._gcode_loaded_path = None

        # Configura a barra para mostrar % dentro
        if hasattr(self, "prg_cycle_top"):
            self.prg_cycle_top.setRange(0, 100)
            self.prg_cycle_top.setValue(0)
            self.prg_cycle_top.setTextVisible(True)
            self.prg_cycle_top.setFormat("%p%")

        # ------ eixos das coordenadas do cabecalho --------
        self.AXIS_X = 0
        self.AXIS_Z = 2

        # ----- conecta o botão MACHINE ON (topo) -----
        # (botão do topo: "MACHINE ⏻")
        self.btn_machine_off_top.clicked.connect(self.toggle_machine)

        # ----- conecta o botão EMERGÊNCIA (topo) -----
        # TROQUE "btn_emerg_top" pelo nome REAL do botão no .ui
        self.btn_emergencia_bottom.clicked.connect(self.toggle_estop)

        #----- conecta o botao START/PAUSE  (Cycle start/Pause) -----
        # ---- botao iniciar / pausa ------
        self.btn_start_cycle.clicked.connect(self.cycle_start_toggle)

        # ----- botao stop ------
        self.btn_stop_cycle.clicked.connect(self.cycle_stop)

        # ----- timer para atualizar LEDs / estados da aba MANUT -----
        self.status_timer = QtCore.QTimer(self)
        self.status_timer.timeout.connect(self.update_status_panel)
        self.status_timer.start(200)  # 200 ms

        self.cloud = IceqCloudClient("/home/iceq/linuxcnc/configs/TORNO_ICEQ/iceq_cloud/iceq_cloud_config.json")

        def _cloud_boot():
            if self.cloud.is_configured():
                self.cloud.send_startup_log("IHM ICEQ iniciou (bancada)")
                self.cloud.send_ping()
            else:
                print("[ICEQ][CLOUD] Não configurado: ver iceq_cloud_config.json")

        threading.Thread(target=_cloud_boot, daemon=True).start()

        # =========================================================
        # CLOUD — Timers (ping + transições)
        # =========================================================
        self._cloud_enabled = False
        self._cloud_last_state = None
        self._cloud_last_sent_ts = {}   # rate-limit por evento
        self._cloud_min_interval_s = 1.0  # mínimo entre logs repetidos do mesmo tipo

        try:
            self._cloud_enabled = bool(self.cloud and self.cloud.is_configured())
        except Exception:
            self._cloud_enabled = False

        # Timer de PING (online/heartbeat) — leve e independente da UI
        self._cloud_ping_timer = QtCore.QTimer(self)
        self._cloud_ping_timer.timeout.connect(self._cloud_ping_tick)

        # Timer de transições (gera logs somente quando algo muda)
        self._cloud_transition_timer = QtCore.QTimer(self)
        self._cloud_transition_timer.timeout.connect(self._cloud_transition_tick)

        if self._cloud_enabled:
            # ping a cada 20s (ajuste se quiser)
            self._cloud_ping_timer.start(20000)
            # checagem de transição a cada 500ms (bom compromisso)
            self._cloud_transition_timer.start(500)

        # ----------------------------------------------------
        # INTERTRAVAMENTO VISUAL (pisca/cores nos botões)
        # ----------------------------------------------------

        self._blink_phase = False
        self._blink_timer = QtCore.QTimer(self)
        self._blink_timer.timeout.connect(self._blink_tick)
        self._blink_timer.start(400)  # ms (pisca ~2.5Hz)


        # Guarda estilos e tamanhos originais para poder voltar ao "normal" sem mexer no layout
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
                        int(btn.minimumWidth()),
                        int(btn.minimumHeight()),
                        int(btn.maximumWidth()),
                        int(btn.maximumHeight()),
                    )
                except Exception:
                    self._btn_size_default[_btn_name] = None


        # Tolerância de AT SPEED (em RPM): usa o maior entre abs e percentual
        self._sp_at_speed_tol_abs_rpm = 50.0
        self._sp_at_speed_tol_pct = 0.05
        self._sp_at_speed_required_lo = 0.0
        self._sp_at_speed_required_hi = 0.0
        self._sp_at_speed_required_hits = 3
        self._sp_at_speed_cnt = 0
        self._sp_at_speed_last = False

        # faz uma atualização inicial dos LEDs
        self.update_status_panel()

        # ------ botao de abrir programa ---------
        # botao de abrir no editor
        self.btn_open_program_edit.clicked.connect(self.open_program)
        # botao de abrir no visualizador de g code
        self.btn_open_program_main.clicked.connect(self.open_program)

        # -----------------------------
        # Ferramentas T1..T16 (torre 8 pos + 8 virtuais)
        # -----------------------------
        self._init_tool_buttons()
        self._set_tool_buttons_enabled(False)  # inicia travado (máquina iniciando)

        # ---------------------------------------------------------
        # MDI (executar comandos e manter histórico)
        # ---------------------------------------------------------
        if hasattr(self, "btn_mdi_send"):
            self.btn_mdi_send.clicked.connect(self.on_mdi_send)

        if hasattr(self, "txt_mdi_entry"):
            # Enter no teclado virtual/físico envia o comando
            try:
                self.txt_mdi_entry.returnPressed.connect(self.on_mdi_send)
            except Exception:
                pass

        if hasattr(self, "txt_mdi_history"):
            try:
                # mantém histórico “selecionável” porém sem edição acidental
                self.txt_mdi_history.setReadOnly(True)
            except Exception:
                pass

            try:
                # QPlainTextEdit recebe eventos no viewport; instalamos em ambos por robustez
                self.txt_mdi_history.installEventFilter(self)
                self.txt_mdi_history.viewport().installEventFilter(self)
            except Exception:
                pass
            # Carrega histórico persistente do MDI (igual AXIS)
            try:
                self._mdi_hist_max_lines = 400
                self._mdi_history_loaded = False
                self._mdi_history_load()
            except Exception as e:
                print(f"[ICEQ] MDI history load: erro: {e}")


        if hasattr(self, "txt_mdi_history"):
            try:
                font = self.txt_mdi_history.font()
                font.setPointSize(15)   # ← ajuste aqui (12 ou 13 costuma ficar ótimo em touch)
                self.txt_mdi_history.setFont(font)
            except Exception:
                pass



        # ------------------------------------------------------------
        # SPINDLE / COOLANT — ESTADO INTERNO (fonte única de verdade)
        # ------------------------------------------------------------
        self._spindle_rpm_setpoint = 0
        self._spindle_dir = 0            # +1=CW | -1=CCW | 0=STOP
        self._spindle_running = False
        self._coolant_on = False

        self._spindle_step = 100          # ajuste fino depois se quiser

        # --- AT SPEED (encoder feedback) ---
        # Contador para "debounce": só considera AT SPEED verdadeiro após N leituras consecutivas
        self._sp_at_speed_cnt = 0
        self._sp_at_speed_required_hits = 3  # 3 * 200ms = ~600ms (ajuste se quiser)
        self._sp_at_speed_last = False


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

        # ----- botoes de referencia -----
        self.btn_ref_all.clicked.connect(self.ref_all)
        self.btn_ref_x.clicked.connect(self.ref_x)

        if HAS_Y_AXIS:
            self.btn_ref_y.clicked.connect(self.ref_y)
            self.btn_ref_y.setEnabled(True)
        else:
            # ainda nao usamos Y nesta maquina
            self.btn_ref_y.setEnabled(False)

        self.btn_ref_z.clicked.connect(self.ref_z)
        self.btn_zero_peca_g54.clicked.connect(self.zero_g54)

        # ------------------------------------------------------------
        # OVERRIDES (Vel. Máquina / Spindle) - init padrão 100%
        # ranges: 0..120
        # ------------------------------------------------------------
        self._machine_ovr_pct = 100
        self._spindle_ovr_pct = 100

        # ----- ranges + valores iniciais -----
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

        # ----- desconecta qualquer ligação antiga e reconecta com debug -----
        if hasattr(self, "sld_vel_machine_oper"):
            try:
                self.sld_vel_machine_oper.valueChanged.disconnect()
            except Exception:
                pass
            self.sld_vel_machine_oper.valueChanged.connect(self._dbg_machine_ovr_changed)
            print("[ICEQ][DBG] conectado: sld_vel_machine_oper.valueChanged -> _dbg_machine_ovr_changed")

        if hasattr(self, "spn_vel_machine_oper"):
            try:
                self.spn_vel_machine_oper.valueChanged.disconnect()
            except Exception:
                pass
            self.spn_vel_machine_oper.valueChanged.connect(self.on_machine_ovr_spin)
            print("[ICEQ][DBG] conectado: spn_vel_machine_oper.valueChanged -> on_machine_ovr_spin")

        if hasattr(self, "sld_vel_spindle_oper"):
            try:
                self.sld_vel_spindle_oper.valueChanged.disconnect()
            except Exception:
                pass
            self.sld_vel_spindle_oper.valueChanged.connect(self._dbg_spindle_ovr_changed)
            print("[ICEQ][DBG] conectado: sld_vel_spindle_oper.valueChanged -> _dbg_spindle_ovr_changed")

        if hasattr(self, "spn_vel_spindle_oper"):
            try:
                self.spn_vel_spindle_oper.valueChanged.disconnect()
            except Exception:
                pass
            self.spn_vel_spindle_oper.valueChanged.connect(self._dbg_spindle_ovr_spin_changed)
            print("[ICEQ][DBG] conectado: spn_vel_spindle_oper.valueChanged -> _dbg_spindle_ovr_spin_changed")

        # ----- aplica defaults (100% / 100%) -----
        self._apply_machine_override_pct(100)
        self._apply_spindle_override_pct(100)

        # ------------------------------------------------------------
        # JOG (BÁSICO por CLIQUE) - X e Z via MDI em THREAD (não trava UI)
        # ------------------------------------------------------------
        self._jog_busy = False
        self._jog_step_mm_default = 0.1  # passo default se não conseguir ler do combo

        # Finalização determinística do JOG (feito pela GUI, não pelo worker)
        self._jog_finish_timer = QtCore.QTimer(self)
        self._jog_finish_timer.timeout.connect(self._jog_finish_tick)
        self._jog_finish_axis = ""
        self._jog_finish_deadline = 0.0

        # Conecta botões de jog (clique simples)
        if hasattr(self, "btn_jog_x_plus"):
            self.btn_jog_x_plus.clicked.connect(lambda: self._jog_click("X", +1))
        if hasattr(self, "btn_jog_x_minus"):
            self.btn_jog_x_minus.clicked.connect(lambda: self._jog_click("X", -1))

        if hasattr(self, "btn_jog_z_plus"):
            self.btn_jog_z_plus.clicked.connect(lambda: self._jog_click("Z", +1))
        if hasattr(self, "btn_jog_z_minus"):
            self.btn_jog_z_minus.clicked.connect(lambda: self._jog_click("Z", -1))


            # CONTÍNUO (sem mexer no incremental): pressed/released só atuam se modo estiver em CONTÍNUO
            # self.btn_jog_x_plus.pressed.connect(lambda: self._jog_press("X", +1))
            # self.btn_jog_x_plus.released.connect(lambda: self._jog_release("X"))
            # self.btn_jog_x_minus.pressed.connect(lambda: self._jog_press("X", -1))
            # self.btn_jog_x_minus.released.connect(lambda: self._jog_release("X"))

            # self.btn_jog_z_plus.pressed.connect(lambda: self._jog_press("Z", +1))
            # self.btn_jog_z_plus.released.connect(lambda: self._jog_release("Z"))
            # self.btn_jog_z_minus.pressed.connect(lambda: self._jog_press("Z", -1))
            # self.btn_jog_z_minus.released.connect(lambda: self._jog_release("Z"))


        # ------------------------------------------------------------
        # JOG Contínuo (press & hold) - HALUI
        # Não remove o clique do incremental; apenas adiciona pressed/released.
        # ------------------------------------------------------------
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


        # ------------------------------------------------------------
        # JOG - Modo (AGORA: btn_jog_mode): Contínuo / Incremental
        # Regra: enquanto estiver em "Contínuo", os botões NÃO respondem.
        # Default: iniciar sempre em "Contínuo".
        # ------------------------------------------------------------
        if hasattr(self, "btn_jog_mode"):
            # tabela de modos e índice atual
            self._jog_mode_options = ["Contínuo", "Incremental"]
            self._jog_mode_idx = 0  # default: Contínuo

            try:
                self.btn_jog_mode.setText(self._jog_mode_options[self._jog_mode_idx])
            except Exception:
                pass

            try:
                self.btn_jog_mode.clicked.connect(self._on_btn_jog_mode_clicked)
            except Exception:
                pass

            # aplica estado inicial (desabilita botões se não for incremental)
            try:
                self._on_jog_mode_changed(self._jog_mode_options[self._jog_mode_idx])
            except Exception:
                pass

        elif hasattr(self, "cb_jog_mode"):
            # compatibilidade (se ainda existir no .ui antigo)
            try:
                self.cb_jog_mode.blockSignals(True)
                self.cb_jog_mode.setCurrentIndex(0)
                self.cb_jog_mode.blockSignals(False)
            except Exception:
                pass

            try:
                self.cb_jog_mode.currentTextChanged.connect(self._on_jog_mode_changed)
            except Exception:
                pass

            try:
                self._on_jog_mode_changed(self.cb_jog_mode.currentText())
            except Exception:
                pass

        # passo atual de JOG (mm)
        self._jog_step_mm = 0.1

        # ------------------------------------------------------------
        # JOG - seleção de passo (AGORA: btn_jog_step)
        # Default: começar em 10mm (como você descreveu)
        # ------------------------------------------------------------
        if hasattr(self, "btn_jog_step"):
            self._jog_step_options = [10.0, 1.0, 0.5, 0.1, 0.01, 0.001]
            self._jog_step_idx = 0  # default: 10mm

            try:
                self.btn_jog_step.setText(f"{self._jog_step_options[self._jog_step_idx]:g} mm")
            except Exception:
                pass

            try:
                self.btn_jog_step.clicked.connect(self._on_btn_jog_step_clicked)
            except Exception:
                pass

            # força leitura inicial (mantém sua lógica atual do passo)
            try:
                self._on_jog_step_changed(str(self._jog_step_options[self._jog_step_idx]))
            except Exception:
                pass

        elif hasattr(self, "cb_jog_step"):
            # compatibilidade (se ainda existir no .ui antigo)
            self.cb_jog_step.currentTextChanged.connect(self._on_jog_step_changed)
            self._on_jog_step_changed(self.cb_jog_step.currentText())

        # ------------------------------------------------------------
        # JOG - Velocidade (slider + spinbox) 0..120% (inicia 100%)
        # - Atualiza SOMENTE o JOG (não mexe no cabeçalho VEL do G-code)
        # - lbl_jog_tittle mostra: VELOCIDADE JOG: <mm/min> mm/min
        # ------------------------------------------------------------


        # ------------------------------------------------------------
        # JOG - Velocidade (slider + spinbox) 0..120% (inicia 100%)
        # - Atualiza SOMENTE o JOG (não mexe no cabeçalho VEL do G-code)
        # - lbl_jog_tittle mostra: VELOCIDADE JOG: <mm/min> mm/min
        # ------------------------------------------------------------
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

        # desconecta ligações antigas e reconecta
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

        # aplica estado inicial (100%)
        self._apply_jog_speed_pct(100)





    # ------------------------------------------------------------
    # OVERRIDES (Feed / Spindle) - handlers + apply + sync
    # ------------------------------------------------------------
    def _apply_machine_override_pct(self, pct: int):
        """Aplica Feed Override (FRO) no LinuxCNC. pct: 0..120"""
        try:
            pct_i = int(max(0, min(120, pct)))
            self._machine_ovr_pct = pct_i
            # LinuxCNC espera fator: 1.00 = 100%
            self.cmd.feedrate(pct_i / 100.0)
        except Exception as e:
            print(f"[ICEQ] feed override erro: {e}")


    def _sync_machine_widgets(self, pct: int):
        """Sincroniza slider e spinbox da Vel. Máquina sem loop."""
        if hasattr(self, "sld_vel_machine_oper") and self.sld_vel_machine_oper.value() != pct:
            self.sld_vel_machine_oper.blockSignals(True)
            self.sld_vel_machine_oper.setValue(pct)
            self.sld_vel_machine_oper.blockSignals(False)

        if hasattr(self, "spn_vel_machine_oper") and self.spn_vel_machine_oper.value() != pct:
            self.spn_vel_machine_oper.blockSignals(True)
            self.spn_vel_machine_oper.setValue(pct)
            self.spn_vel_machine_oper.blockSignals(False)

    def _on_machine_ovr_slider(self, value: int):
        pct = int(max(0, min(120, value)))
        self._sync_machine_widgets(pct)
        self._apply_machine_override_pct(pct)

    def _on_machine_ovr_spin(self, value: int):
        pct = int(max(0, min(120, value)))
        self._sync_machine_widgets(pct)
        self._apply_machine_override_pct(pct)

    # -------- Spindle (override do spindle) --------

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

        # NÃO mexe em lbl_vel_spindle_oper aqui (título fixo “SPINDLE”)

    def _apply_spindle_override_pct(self, pct):
        """
        Aplica spindle override (0..120%) no LinuxCNC.
        Isso NÃO é o "S" do programa: é multiplicador do spindle.
        """
        pct = self._clamp_pct(pct, 0, 120)
        scale = float(pct) / 100.0

        # 1) Preferência: linuxcnc.command()
        try:
            if hasattr(self.cmd, "spindleoverride"):
                self.cmd.spindleoverride(scale)
                return
        except Exception as e:
            print(f"[ICEQ] spindle override via cmd falhou: {e}")

        # 2) Fallback: HAL (se existir writer)
        try:
            import hal

            # nomes comuns em configs com halui
            if hal.pin_has_writer("halui.spindle-override.value"):
                hal.set_p("halui.spindle-override.value", str(scale))
                return

            if hal.pin_has_writer("halui.spindle-override"):
                hal.set_p("halui.spindle-override", str(scale))
                return

        except Exception as e:
            print(f"[ICEQ] spindle override via HAL falhou: {e}")

    # --------------------------------------------------
    # -------- wrappers de debug -----------------------
    # --------------------------------------------------
    def _dbg_machine_ovr_changed(self, v):
        try:
            print(f"[ICEQ][DBG] machine_ovr changed -> {v}")
        except Exception:
            pass
        self.on_machine_ovr_slider(v)

    def _dbg_spindle_ovr_changed(self, v):
        try:
            print(f"[ICEQ][DBG] spindle_ovr changed -> {v}")
        except Exception:
            pass
        self.on_spindle_ovr_slider(v)

    def _dbg_spindle_ovr_spin_changed(self, v):
        try:
            print(f"[ICEQ][DBG] spindle_ovr spin changed -> {v}")
        except Exception:
            pass
        self.on_spindle_ovr_spin(v)


    # ---------------------------------------------------------
    # ----------- HAL float + debug do spindle override -------
    # ---------------------------------------------------------
    def _hal_float(self, pin_name):
        """
        Lê pino HAL float (se existir).
        Retorna float ou None.
        """
        try:
            import hal
            try:
                # linuxcnc hal: hal.get_value pode existir dependendo da build
                v = hal.get_value(pin_name)
                return float(v)
            except Exception:
                pass

            # fallback: cria "pin handle" se o hal expuser isso (nem sempre)
            try:
                p = hal.Pin(pin_name)
                return float(p.get())
            except Exception:
                return None
        except Exception:
            return None

    def _hal_set_float(self, pin_name, value):
        """
        Escreve em pino HAL float (se existir e for writer).
        Retorna True/False.
        """
        try:
            import hal
            try:
                if hasattr(hal, "pin_has_writer") and hal.pin_has_writer(pin_name):
                    hal.set_p(pin_name, str(float(value)))
                    return True
            except Exception:
                pass
        except Exception:
            pass
        return False

    def _hal_sets_signal(self, signal_name, value):
        """
        Define SIGNAL HAL via 'halcmd sets <signal> <value>'.
        Usar isso (sets) é o correto para signals criados via 'net ...',
        principalmente quando o pino do halui é IN (não aceita setp).
        Retorna (ok: bool, err: str).
        """
        try:
            import subprocess

            # bit precisa ser 0/1 (string sem decimal) para não dar "invalid for bit"
            if isinstance(value, bool):
                vtxt = "1" if value else "0"
            elif isinstance(value, int):
                vtxt = str(int(value))
            else:
                # float / str numérica
                try:
                    vtxt = str(float(value))
                except Exception:
                    vtxt = str(value)

            r = subprocess.run(
                ["halcmd", "sets", str(signal_name), vtxt],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            if r.returncode != 0:
                err = (r.stderr or r.stdout or "").strip()
                return False, err
            return True, ""
        except Exception as e:
            return False, str(e)

    def _hal_setp_pin(self, pin_name, value):
        """
        Seta um PIN/parametro HAL via 'halcmd setp' (necessário para pinos tipo motion.teleop-enable).
        Retorna (ok, err).
        """
        try:
            name = str(pin_name).strip()
            v = value

            # Normaliza bool -> 0/1
            if isinstance(v, bool):
                v = 1 if v else 0

            # halcmd setp aceita string numérica
            cmd = ['halcmd', 'setp', name, str(v)]
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if p.returncode != 0:
                err = (p.stderr or p.stdout or '').strip()
                return False, err
            return True, ''
        except Exception as e:
            return False, str(e)




    # ----------------------------------------------------------
    #   Função para acender/apagar um "LED" (QFrame quadradinho)
    # ----------------------------------------------------------
    def set_led(self, frame, is_on):
        """Muda a cor do QFrame: verde ligado, vermelho escuro desligado."""
        if is_on:
            frame.setStyleSheet(
                "background-color: rgb(0, 255, 0);"
                "border: 1px solid black;"
            )
        else:
            frame.setStyleSheet(
                "background-color: rgb(255, 0, 0);"
                "border: 1px solid black;"
            )

    # ----------------------------------------------------------
    #   INTERTRAVAMENTO VISUAL (cores/pisca nos botões principais)
    # ----------------------------------------------------------
    def _blink_tick(self):
        """Timer de pisca (não mexe em lógica de máquina, só visual)."""
        try:
            self._blink_phase = not bool(getattr(self, "_blink_phase", False))
            # Atualiza visual mesmo sem novo poll (o update_status_panel roda a cada 200ms)
            self._update_interlock_visuals()
        except Exception:
            pass

    def _btn_qss_bg(self, rgb_text):
        """QSS sólido SEM alterar sizeHint (não mexe em padding no :pressed)."""
        return (
            "QPushButton{"
            f"background-color: {rgb_text};"
            "border: 1px solid rgb(80, 80, 80);"
            "padding: 0px;"
            "margin: 0px;"
            "}"
            "QPushButton:pressed{"
            f"background-color: {rgb_text};"
            "padding: 0px;"
            "margin: 0px;"
            "}"
        )


    def _btn_set_visual(self, btn_attr, mode):
        """
        mode:
          - "default"
          - "solid_green"
          - "blink_red"
          - "blink_yellow"
        """
        if not hasattr(self, btn_attr):
            return

        btn = getattr(self, btn_attr)
        # Reforça tamanhos originais para evitar "pulo" de layout quando muda QSS
        try:
            sz = getattr(self, "_btn_size_default", {}).get(btn_attr, None)
            if sz:
                btn.setMinimumSize(sz[0], sz[1])
                btn.setMaximumSize(sz[2], sz[3])
        except Exception:
            pass

        base = ""
        try:
            base = self._btn_style_default.get(btn_attr, "")
        except Exception:
            base = ""

        if mode == "default":
            btn.setStyleSheet(base)
            return

        if mode == "solid_green":
            btn.setStyleSheet(self._btn_qss_bg("rgb(0, 200, 0)"))
            return

        if mode == "blink_red":
            if bool(getattr(self, "_blink_phase", False)):
                btn.setStyleSheet(self._btn_qss_bg("rgb(220, 0, 0)"))
            else:
                btn.setStyleSheet(base)
            return

        if mode == "blink_yellow":
            if bool(getattr(self, "_blink_phase", False)):
                btn.setStyleSheet(self._btn_qss_bg("rgb(255, 200, 0)"))
            else:
                btn.setStyleSheet(base)
            return

        # fallback
        btn.setStyleSheet(base)

    def _update_interlock_visuals(self):
        """Aplica o "fluxo" visual: EMERG -> MACHINE -> REF -> START/PAUSE."""
        try:
            estop_active = bool(getattr(self.stat, "estop", False))
            enabled = bool(getattr(self.stat, "enabled", False))

            # homing (usa somente X/Z do seu setup: joints 0 e 1)
            homed_all = False
            try:
                h = getattr(self.stat, "homed", None)
                if h is not None and len(h) >= 2:
                    homed_all = bool(h[0]) and bool(h[1])
            except Exception:
                homed_all = False

            # ciclo (AUTO rodando / pausado)
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

            # 1) EMERGÊNCIA
            if estop_active:
                self._btn_set_visual("btn_emergencia_bottom", "blink_red")
                self._btn_set_visual("btn_machine_off_top", "default")
                self._btn_set_visual("btn_ref_all", "default")
                self._btn_set_visual("btn_start_cycle", "default")
                return

            # E-STOP limpo
            self._btn_set_visual("btn_emergencia_bottom", "solid_green")

            # 2) MACHINE ON
            if not enabled:
                self._btn_set_visual("btn_machine_off_top", "blink_yellow")
                self._btn_set_visual("btn_ref_all", "default")
                self._btn_set_visual("btn_start_cycle", "default")
                return

            # MACHINE ligada
            self._btn_set_visual("btn_machine_off_top", "solid_green")

            # 3) REF ALL (Homing)
            if bool(getattr(self, "_homing_busy", False)):
                self._btn_set_visual("btn_ref_all", "blink_yellow")
                self._btn_set_visual("btn_start_cycle", "default")
                return

            if not homed_all:
                self._btn_set_visual("btn_ref_all", "blink_yellow")
                self._btn_set_visual("btn_start_cycle", "default")
                return

            # Homing concluído
            self._btn_set_visual("btn_ref_all", "solid_green")

            # 4) START/PAUSE
            if program_active:
                if paused:
                    self._btn_set_visual("btn_start_cycle", "blink_yellow")
                else:
                    self._btn_set_visual("btn_start_cycle", "solid_green")
            else:
                self._btn_set_visual("btn_start_cycle", "default")

        except Exception:
            # Nunca deixa uma falha visual travar a IHM
            pass


    # ----------------------------------------------------------
    #   Botão EMERGÊNCIA (Liga / Desliga E-STOP lógico)
    # ----------------------------------------------------------
    def toggle_estop(self):
        """
        Se E-STOP estiver ativo (estop=1):
            -> manda STATE_ESTOP_RESET (sai da emergência)
        Se E-STOP estiver inativo (estop=0):
            -> manda STATE_ESTOP (entra em emergência)
        """
        try:
            self.stat.poll()
        except Exception as e:
            print(f"[ICEQ] toggle_estop: erro no stat.poll(): {e}")
            return

        estop = bool(self.stat.estop)
        print(f"[ICEQ] toggle_estop: estop={estop}")

        if estop:
            print("[ICEQ] toggle_estop: resetando E-STOP (STATE_ESTOP_RESET)")
            self.cmd.state(linuxcnc.STATE_ESTOP_RESET)
        else:
            print("[ICEQ] toggle_estop: ativando E-STOP (STATE_ESTOP)")
            self.cmd.state(linuxcnc.STATE_ESTOP)

    # ----------------------------------------------------------
    #   Botão MACHINE (liga/desliga máquina)
    # ----------------------------------------------------------
    def toggle_machine(self):
        """
        Lógica:
          - Se estiver em E-STOP -> NÃO faz nada (só avisa no terminal).
          - Se não estiver em E-STOP:
                * se não estiver enabled  -> STATE_ON
                * se já estiver enabled   -> STATE_OFF
        """
        try:
            self.stat.poll()
        except Exception as e:
            print(f"[ICEQ] toggle_machine: erro no stat.poll(): {e}")
            return

        estop = bool(self.stat.estop)
        enabled = bool(self.stat.enabled)

        print(f"[ICEQ] toggle_machine: estop={estop} enabled={enabled}")

        if estop:
            print("[ICEQ] MACHINE: em E-STOP, não vou habilitar. "
                  "Use o botão EMERGÊNCIA para resetar.")
            return

        if not enabled:
            print("[ICEQ] ligando máquina (STATE_ON)")
            self.cmd.state(linuxcnc.STATE_ON)
        else:
            print("[ICEQ] desligando máquina (STATE_OFF)")
            self.cmd.state(linuxcnc.STATE_OFF)

    # ----- Botão INICIAR/PAUSAR (Cycle start / Pause / Resume) -----
    def cycle_start_toggle(self):
        """
        Comportamento do botão INICIAR/PAUSAR:

        - Se estiver em E-STOP ou máquina desligada -> ignora.
        - Se programa estiver PAUSADO              -> AUTO_RESUME.
        - Se programa estiver RODANDO              -> AUTO_PAUSE.
        - Se programa estiver PARADO / IDLE        -> AUTO_RUN (linha 0).
        """

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

        print(f"[ICEQ] cycle_start: estop={estop} enabled={enabled} "
              f"mode={mode} interp={interp} paused={paused}")

        # Segurança: se estiver em E-STOP ou máquina OFF, não faz nada
        if estop or not enabled:
            print("[ICEQ] cycle_start: ignorado (E-STOP ativo ou máquina OFF).")
            return

        # Só troca para AUTO se ainda NÃO estiver em AUTO
        if mode != linuxcnc.MODE_AUTO:
            try:
                self.cmd.mode(linuxcnc.MODE_AUTO)
                self.cmd.wait_complete()
            except Exception as e:
                print(f"[ICEQ] cycle_start: erro ao mudar para MODE_AUTO: {e}")
                return

        # --- Lógica de estados equivalente ao halui.program.* ---

        # 1) Se estiver PAUSADO -> RESUME
        if paused:
            print("[ICEQ] cycle_start: AUTO_RESUME")
            try:
                self.cmd.auto(linuxcnc.AUTO_RESUME)
            except Exception as e:
                print(f"[ICEQ] cycle_start: erro no AUTO_RESUME: {e}")
            return

        # 2) Não pausado: checamos se está rodando
        #    Consideramos rodando se o interp NÃO for IDLE
        running = (interp != linuxcnc.INTERP_IDLE)

        if running:
            # 2a) Se estiver RODANDO -> PAUSE
            print("[ICEQ] cycle_start: AUTO_PAUSE")
            try:
                self.cmd.auto(linuxcnc.AUTO_PAUSE)
            except Exception as e:
                print(f"[ICEQ] cycle_start: erro no AUTO_PAUSE: {e}")
        else:
            # 3) Caso contrário -> RUN desde a linha 0
            print("[ICEQ] cycle_start: AUTO_RUN (linha 0)")
            try:
                self.cmd.auto(linuxcnc.AUTO_RUN, 0)
            except Exception as e:
                print(f"[ICEQ] cycle_start: erro no AUTO_RUN: {e}")

    # ----------------------------------------------------------
    #   Botão STOP (para o programa atual)
    # ----------------------------------------------------------
    def cycle_stop(self):
        """
        STOP do programa:
          - Garante modo AUTO
          - Manda AUTO_ABORT (para e volta para o início do programa)
        """
        try:
            print("[ICEQ] cycle_stop: abort()")
            # abort pode ser chamado em qualquer estado
            self.cmd.abort()
        except Exception as e:
            print(f"[ICEQ] cycle_stop: erro no abort(): {e}")

       # print("[ICEQ] cycle_stop: AUTO_ABORT")
       # self.cmd.mode(linuxcnc.MODE_AUTO)
       # self.cmd.auto(linuxcnc.AUTO_ABORT)

    # ============================================================
    # OVERRIDE: Vel. Máquina (Feed + Rapid) e Spindle (Spindle Override)
    # ============================================================

    def _clamp_pct(self, v, lo=0, hi=120):
        try:
            v = int(v)
        except Exception:
            v = 100
        if v < lo:
            return lo
        if v > hi:
            return hi
        return v

    # -------- Vel. Máquina (override geral) --------

    def on_machine_ovr_slider(self, value):
        pct = self._clamp_pct(value, 0, 120)
        self._sync_machine_widgets(pct)
        self._apply_machine_override_pct(pct)

    def on_machine_ovr_spin(self, value):
        pct = self._clamp_pct(value, 0, 120)
        self._sync_machine_widgets(pct)
        self._apply_machine_override_pct(pct)

    def _sync_machine_widgets(self, pct):
        self._machine_ovr_pct = pct

        if hasattr(self, "sld_vel_machine_oper"):
            self.sld_vel_machine_oper.blockSignals(True)
            self.sld_vel_machine_oper.setValue(pct)
            self.sld_vel_machine_oper.blockSignals(False)

        if hasattr(self, "spn_vel_machine_oper"):
            self.spn_vel_machine_oper.blockSignals(True)
            self.spn_vel_machine_oper.setValue(pct)
            self.spn_vel_machine_oper.blockSignals(False)

        # Se você tiver label de texto (opcional)
        if hasattr(self, "_machine_ovr_pct"):
            try:
                self._sync_machine_widgets(int(self._machine_ovr_pct))
            except Exception:
                pass

    def _apply_machine_override_pct(self, pct):
        """
        Aplica override geral: Feed override + Rapid override.
        Em IHMs tipo Axis, isso impacta avanço/jog/tempo de movimentos.
        """
        pct = self._clamp_pct(pct, 0, 120)
        scale = float(pct) / 100.0

        # 1) Tenta via linuxcnc.command() (preferência)
        try:
            # feed override
            if hasattr(self.cmd, "feedrate"):
                self.cmd.feedrate(scale)
            # rapid override
            if hasattr(self.cmd, "rapidrate"):
                self.cmd.rapidrate(scale)
            return
        except Exception as e:
            print(f"[ICEQ] machine override via cmd falhou: {e}")

        # 2) Fallback via HAL pins do halui (se existir no seu setup)
        try:
            import hal
            # nomes típicos do HALUI (podem variar conforme config)
            # - halui.feed-override.value
            # - halui.rapid-override.value
            if hal.pin_has_writer("halui.feed-override.value"):
                hal.set_p("halui.feed-override.value", str(scale))
            if hal.pin_has_writer("halui.rapid-override.value"):
                hal.set_p("halui.rapid-override.value", str(scale))
        except Exception as e:
            print(f"[ICEQ] machine override via halui falhou: {e}")

    # -------- Spindle (override só do spindle) --------

    def on_spindle_ovr_slider(self, value):
        pct = self._clamp_pct(value, 0, 120)
        self._sync_spindle_widgets(pct)
        # IMPORTANTE: 0% NÃO dá spindle_stop; só override = 0
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

        # Se você tiver label de texto (opcional)
        if hasattr(self, "_spindle_ovr_pct"):
            try:
                self._sync_spindle_widgets(int(self._spindle_ovr_pct))
            except Exception:
                pass

    def _apply_spindle_override_pct(self, pct):
        """
        Aplica spindle override (somente spindle).
        0% é permitido e NÃO deve emitir spindle_stop.
        """
        pct = self._clamp_pct(pct, 0, 120)
        scale = float(pct) / 100.0

        # 1) Tenta via linuxcnc.command()
        try:
            if hasattr(self.cmd, "spindleoverride"):
                self.cmd.spindleoverride(scale)
                return
        except Exception as e:
            print(f"[ICEQ] spindle override via cmd falhou: {e}")

        # 2) Fallback via HALUI (se existir no seu setup)
        try:
            import hal
            # nomes típicos (podem variar)
            # ex.: halui.spindle.0.override.value
            if hal.pin_has_writer("halui.spindle.0.override.value"):
                hal.set_p("halui.spindle.0.override.value", str(scale))
        except Exception as e:
            print(f"[ICEQ] spindle override via halui falhou: {e}")

    # -----------------------------
    # Ferramentas T1..T16
    # -----------------------------
    def _init_tool_buttons(self):
        """
        Inicializa botões btn_1..btn_16, label de ferramenta ativa e estado interno.
        """
        # Guarda o QSS original de cada botão para permitir realce do 'tool ativo'
        # sem alterar tamanhos/layout.
        self._tool_btn_qss_default = {}
        self._tool_btn_active_last = None

        self._tool_buttons = []

        for n in range(1, 17):
            btn_t = getattr(self, f"btn_t{n}", None)
            if btn_t is None:
                print(f"[ICEQ] AVISO: btn_t{n} nao encontrado no .ui")
                continue

            # Garante que clique chame a ferramenta correta
            btn_t.clicked.connect(lambda checked=False, nn=n: self._tool_request(nn))
            # Snapshot do estilo original (para voltar ao normal quando não ativo)
            try:
                self._tool_btn_qss_default[int(n)] = str(btn_t.styleSheet() or "")
            except Exception:
                pass


            # OpacityEffect (evita "pisca" e funciona em qualquer estilo/tema)
            try:
                eff = QtWidgets.QGraphicsOpacityEffect(btn_t)
                eff.setOpacity(1.0)
                btn_t.setGraphicsEffect(eff)
            except Exception:
                eff = None

            self._tool_buttons.append((n, btn_t, eff))

        self._tool_active_last = None
        self._tool_buttons_locked_reason = ""

        # Fallback: como tool_in_spindle pode não atualizar em setups com M6 remap,
        # mantemos um "tool ativo virtual" para UI.
        self._tool_active_virtual = 0

        # Estado da troca de ferramenta (evita travar UI e evita clique duplo)
        self._toolchange_busy = False
        self._toolchange_thread = None
        self._toolchange_req_tool = 0
        self._toolchange_last_ok = True
        self._toolchange_last_error = ""
        self._toolchange_started_ts = 0.0
        self._toolchange_timeout_s = 45.0  # ajuste se necessário
        self._toolchange_lock_min_s = 0.8
        self._mdi_pending_tool = 0


        # Ajuste visual inicial (apagado/travado)
        self._set_tool_buttons_enabled(False)
        self._update_active_tool_label(None)

        # ---------------------------------------------
        # Toolchange (intertravamento real)
        # ---------------------------------------------
        # - self._toolchange_busy = False
        # - self._toolchange_target = 0
        # - self._toolchange_thread = None
        self._toolchange_busy = False

    def _update_active_tool_label(self, tool_num):
        """
        Atualiza lbl_active_tool_title: 'Ferramenta Ativa: Tn'
        """
        lbl = getattr(self, "lbl_active_tool_title", None)
        if lbl is None:
            return

        if tool_num is None or int(tool_num) <= 0:
            lbl.setText("Ferramenta Ativa: --")
        else:
            lbl.setText(f"Ferramenta Ativa: T{int(tool_num)}")

    def _update_active_tool_button_style(self, active_tool):
        """
        Realça (cor) o botão da ferramenta ativa atual (T1..T16), sem mexer em tamanhos.
        - Usa o QSS original salvo em _tool_btn_qss_default para restaurar o visual padrão.
        - Aplica apenas cor/fonte para destacar o botão ativo.
        """
        try:
            at = int(active_tool)
        except Exception:
            at = 0

        # evita re-aplicar QSS a cada tick
        if at == getattr(self, "_tool_btn_active_last", None):
            return
        self._tool_btn_active_last = at

        for item in getattr(self, "_tool_buttons", []):
            try:
                if len(item) == 2:
                    n, btn = item
                else:
                    n, btn, _eff = item

                try:
                    base = str(getattr(self, "_tool_btn_qss_default", {}).get(int(n), "") or "")
                except Exception:
                    base = str(btn.styleSheet() or "")

                if at > 0 and int(n) == at:
                    # Destaque do tool ativo (somente cor + bold)
                    btn.setStyleSheet(base + "\nQPushButton{background-color: rgb(0, 160, 0); color: white; font-weight: bold;}")
                else:
                    # Volta ao estilo original
                    btn.setStyleSheet(base)

            except Exception:
                pass


    def _set_tool_buttons_enabled(self, enabled: bool):
        """
        Lock DURO dos botões de ferramenta:
        - setEnabled
        - bloqueia eventos de mouse/touch
        - efeito visual consistente
        """
        ena = bool(enabled)

        for item in getattr(self, "_tool_buttons", []):
            try:
                if len(item) == 2:
                    n, btn = item
                    eff = None
                else:
                    n, btn, eff = item

                # 1) Enable/disable lógico
                btn.setEnabled(ena)

                # 2) BLOQUEIO DURO de mouse/touch
                btn.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, not ena)

                # 3) Visual
                if eff is not None:
                    eff.setOpacity(1.0 if ena else 0.25)

            except Exception:
                pass

    def _toolchange_hw_active(self) -> bool:
        """
        Detecta troca em andamento pelo hardware.

        IMPORTANTE:
        - NÃO use iocontrol.0.user-enable-out aqui.
          Esse sinal costuma ficar TRUE com a máquina habilitada e "mata" a FSM.
        - Use apenas sinais que realmente representem a troca (P6/P7 ou saídas reais).
        """
        pins = (
            # M64/M65 P6/P7 (caso esteja realmente mapeado assim no seu setup)
            "motion.digital-out-06",
            "motion.digital-out-07",

            # Fallbacks comuns (adicione aqui SOMENTE sinais que você confirmar que mudam na troca)
            # "parport.0.pin-XX-out",
            # "classicladder.0.out-XX",
        )

        for p in pins:
            v = self._hal_bit(p)
            if v is not None and bool(v):
                return True

        return False


    
    def _start_toolchange_thread(self, tool_num: int):
        """
        FSM não-bloqueante (QTimer) para executar Tn M6 sem travar a UI.
        Mantém _toolchange_busy True enquanto a troca está em andamento.
        """
        # Se já existe FSM rodando, ignora
        if bool(getattr(self, "_toolchange_busy", False)):
            # Se busy mas não há timer ativo, permite reiniciar
            tmr = getattr(self, "_toolchange_fsm_timer", None)
            if tmr is not None and tmr.isActive():
                print("[ICEQ] Troca já em andamento (FSM). Ignorando novo pedido.")
                return

        try:
            self._toolchange_busy = True
            self._toolchange_req_tool = int(tool_num)
            self._toolchange_last_ok = True
            self._toolchange_last_error = ""

            # Guarda modo anterior para restaurar ao final (não prender em MDI)
            try:
                self.stat.poll()
                self._toolchange_prev_mode = int(getattr(self.stat, "task_mode", linuxcnc.MODE_MANUAL))
            except Exception:
                self._toolchange_prev_mode = linuxcnc.MODE_MANUAL

            self._toolchange_fsm_state = "MDI_MODE"
            self._toolchange_fsm_sent = False
            self._toolchange_sent_ts = 0.0
            self._toolchange_force_off_done = False

            self._toolchange_deadline = time.time() + float(getattr(self, "_toolchange_timeout_s", 45.0))
        except Exception:
            pass


        # UI lock imediato
        try:
            self._set_tool_buttons_enabled(False)
        except Exception:
            pass

        # Timer da FSM
        try:
            if not hasattr(self, "_toolchange_fsm_timer") or self._toolchange_fsm_timer is None:
                self._toolchange_fsm_timer = QtCore.QTimer(self)
                self._toolchange_fsm_timer.timeout.connect(self._toolchange_fsm_tick)
            self._toolchange_fsm_timer.start(50)  # 50ms
            print(f"[ICEQ] Toolchange FSM start: T{int(tool_num)}")
        except Exception as e:
            self._toolchange_last_ok = False
            self._toolchange_last_error = str(e)
            self._toolchange_busy = False
            print(f"[ICEQ] ERRO iniciando FSM de troca: {e}")

    def _toolchange_fsm_tick(self):
        """
        Tick da FSM de toolchange. Não usa wait_complete(). Não bloqueia a UI.
        Critério de fim: hardware P6/P7 desligarem (e timeout).
        """
        try:
            # Timeout duro
            if time.time() > float(getattr(self, "_toolchange_deadline", 0.0)):
                raise RuntimeError("timeout aguardando fim da troca (FSM)")

            # Sempre poll aqui (rápido)
            try:
                self.stat.poll()
            except Exception:
                pass

            # DBG leve (1x por segundo) para identificar quais pinos mudam na troca
            try:
                tdbg = float(getattr(self, "_toolchange_dbg_ts", 0.0))
            except Exception:
                tdbg = 0.0

            if (time.time() - tdbg) > 1.0:
                self._toolchange_dbg_ts = time.time()
                p6 = self._hal_bit("motion.digital-out-06")
                p7 = self._hal_bit("motion.digital-out-07")
                print(f"[ICEQ][TC][DBG] state={getattr(self, '_toolchange_fsm_state', '')} DO6={p6} DO7={p7}")


            st = str(getattr(self, "_toolchange_fsm_state", ""))

            # 1) Garantir modo MDI (sem wait_complete)
            if st == "MDI_MODE":
                try:
                    self.cmd.mode(linuxcnc.MODE_MDI)
                except Exception:
                    pass
                # Avança assim que task_mode reportar MDI (ou após 2-3 ticks)
                try:
                    if int(getattr(self.stat, "task_mode", -1)) == int(linuxcnc.MODE_MDI):
                        self._toolchange_fsm_state = "SEND_M6"
                        self._toolchange_fsm_sent = False
                    else:
                        # ainda aguardando refletir
                        return
                except Exception:
                    # Se não conseguir ler, tenta enviar mesmo assim no próximo estado
                    self._toolchange_fsm_state = "SEND_M6"
                    self._toolchange_fsm_sent = False
                    return

            # 2) Enviar Tn M6 uma única vez
            if st == "SEND_M6":
                if not bool(getattr(self, "_toolchange_fsm_sent", False)):
                    tn = int(getattr(self, "_toolchange_req_tool", 0))
                    self.cmd.mdi(f"T{int(tn)} M6")
                    self._toolchange_fsm_sent = True
                    self._toolchange_sent_ts = time.time()
                    self._toolchange_force_off_done = False
                    self._toolchange_fsm_state = "WAIT_INTERP_IDLE"

                return


            if st == "WAIT_INTERP_IDLE":
                # Espera o interpreter terminar o MDI (igual filosofia do Cycle Start)
                try:
                    interp = int(getattr(self.stat, "interp_state", linuxcnc.INTERP_IDLE))
                except Exception:
                    interp = linuxcnc.INTERP_IDLE

                if interp != linuxcnc.INTERP_IDLE:
                    return

                # Interpreter está IDLE: considera concluído
                tn = int(getattr(self, "_toolchange_req_tool", 0))
                try:
                    self._tool_active_virtual = int(tn)
                except Exception:
                    self._tool_active_virtual = 0

                self._toolchange_last_ok = True
                self._toolchange_last_error = ""
                self._toolchange_fsm_state = "DONE"
                return




            # 5) DONE -> para timer e libera
            if st == "DONE":
                try:
                    if hasattr(self, "_toolchange_fsm_timer") and self._toolchange_fsm_timer is not None:
                        self._toolchange_fsm_timer.stop()
                except Exception:
                    pass

                self._toolchange_busy = False
                # Volta ao modo anterior (ou MANUAL) para não prender em MDI / Interp 2
                try:
                    prev = int(getattr(self, "_toolchange_prev_mode", linuxcnc.MODE_MANUAL))
                    back = prev if prev in (linuxcnc.MODE_MANUAL, linuxcnc.MODE_AUTO) else linuxcnc.MODE_MANUAL
                    self.cmd.mode(back)
                except Exception:
                    pass


                # Atualiza label imediatamente
                try:
                    self._update_active_tool_label(int(getattr(self, "_tool_active_virtual", 0)))
                except Exception:
                    pass

                # Libera botões conforme interlock
                try:
                    ok2, _r2 = self._compute_tools_interlock()
                    self._set_tool_buttons_enabled(bool(ok2))
                except Exception:
                    self._set_tool_buttons_enabled(False)

                return

        except Exception as e:
            # Falha -> abort, para timer e libera (com motivo)
            try:
                self._toolchange_last_ok = False
                self._toolchange_last_error = str(e)
            except Exception:
                pass

            print(f"[ICEQ] ERRO troca ferramenta (FSM): {e}")

            try:
                if hasattr(self.cmd, "abort"):
                    self.cmd.abort()
            except Exception:
                pass

            try:
                if hasattr(self, "_toolchange_fsm_timer") and self._toolchange_fsm_timer is not None:
                    self._toolchange_fsm_timer.stop()
            except Exception:
                pass

            self._toolchange_busy = False

            try:
                ok2, _r2 = self._compute_tools_interlock()
                self._set_tool_buttons_enabled(bool(ok2))
            except Exception:
                self._set_tool_buttons_enabled(False)

    def _toolchange_worker(self, tool_num: int, timeout_s: float):
        err = ""
        ok = False

        try:
            # Guarda modo anterior (para voltar ao final e não “prender” em MDI)
            prev_mode = linuxcnc.MODE_MANUAL
            try:
                self.stat.poll()
                prev_mode = int(getattr(self.stat, "task_mode", linuxcnc.MODE_MANUAL))
            except Exception:
                prev_mode = linuxcnc.MODE_MANUAL

            # Entra em MDI
            self.cmd.mode(linuxcnc.MODE_MDI)
            self.cmd.wait_complete()

            # Executa troca mecânica
            self.cmd.mdi(f"T{int(tool_num)} M6")
            self.cmd.wait_complete()


            # Aguarda término REAL via hardware (P6/P7 desligarem)
            t0 = time.time()
            forced_off_done = False

            while (time.time() - t0) < float(timeout_s):
                if not self._toolchange_hw_active():
                    break

                # Se passou um tempo e ainda está ativo, força M65 (estado final determinístico)
                if (not forced_off_done) and ((time.time() - t0) > 1.0):
                    try:
                        print("[ICEQ] ToolChange: HW ainda ativo, forçando M65 P6/P7...")
                        self.cmd.mdi("M65 P6")
                        self.cmd.wait_complete()
                        self.cmd.mdi("M65 P7")
                        self.cmd.wait_complete()
                        forced_off_done = True
                    except Exception as e:
                        # Se falhar, segue esperando até o timeout (e vai estourar com erro claro)
                        print(f"[ICEQ] ToolChange: falha ao forçar M65 P6/P7: {e}")

                time.sleep(0.05)

            # Se ainda estiver ativo após timeout, falha (para não “liberar” cedo)
            if self._toolchange_hw_active():
                raise RuntimeError("timeout aguardando fim da troca (HW P6/P7 ainda ativo)")

            # Atualiza fallback de UI (tool_in_spindle pode não mudar no seu setup)
            try:
                self._tool_active_virtual = int(tool_num)
            except Exception:
                self._tool_active_virtual = 0

            ok = True

            # Volta para o modo anterior (ou MANUAL) para evitar ficar em Mod 3 Interp 2
            try:
                back_mode = prev_mode if prev_mode in (linuxcnc.MODE_MANUAL, linuxcnc.MODE_AUTO) else linuxcnc.MODE_MANUAL
                self.cmd.mode(back_mode)
                self.cmd.wait_complete()
            except Exception:
                pass

            # Garante interp IDLE (evita “prender” visualmente em INTERP_READING/EXEC)
            t_idle = time.time()
            while (time.time() - t_idle) < 2.0:
                try:
                    self.stat.poll()
                    if int(getattr(self.stat, "interp_state", linuxcnc.INTERP_IDLE)) == linuxcnc.INTERP_IDLE:
                        break
                except Exception:
                    break
                time.sleep(0.05)


        except Exception as e:
            err = str(e)
            print(f"[ICEQ] ERRO troca ferramenta T{tool_num}: {e}")

            try:
                if hasattr(self.cmd, "abort"):
                    self.cmd.abort()
                    self.cmd.wait_complete()
            except Exception:
                pass

        finally:
            # Liberação determinística
            self._toolchange_last_ok = bool(ok)
            self._toolchange_last_error = err
            self._toolchange_busy = False

            # NÃO reabilita aqui para evitar "pisca" (worker + timer brigando).
            # O update_status_panel() fará o enable no próximo tick, com estado estável.
            try:
                QtCore.QTimer.singleShot(0, lambda: None)
            except Exception:
                pass

    def _compute_tools_interlock(self):
        """
        Decide se a troca de ferramentas está liberada.

        Regras (industrial):
        - E-Stop ativo -> bloqueia
        - Máquina OFF -> bloqueia
        - Máquina não referenciada em X/Z -> bloqueia
        - Homing em andamento -> bloqueia
        - Troca de ferramenta em andamento -> bloqueia
        - Programa rodando em AUTO (interp != IDLE) -> bloqueia
        - Interpreter não IDLE (em geral indica execução) -> bloqueia
        """
        try:
            self.stat.poll()
        except Exception:
            return False, "Sem status"

        # 0) Busy interno / hardware (trava determinística)
        if bool(getattr(self, "_toolchange_busy", False)):
            return False, "Troca em andamento"

        # Mesmo se _toolchange_busy cair cedo, travamos enquanto o hardware indicar troca ativa (P6/P7)
        try:
            if bool(self._toolchange_hw_active()):
                return False, "Troca em andamento (HW)"
        except Exception:
            pass

        # 1) E-Stop / máquina ligada
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

        # 2) Referência (somente X/Z – joints 0 e 1)
        # Observação: isso NÃO faz homing do turret; apenas exige X/Z homed para permitir troca.
        try:
            homed_x = bool(self.stat.homed[0]) if hasattr(self.stat, "homed") else False
            homed_z = bool(self.stat.homed[1]) if hasattr(self.stat, "homed") else False
            if not (homed_x and homed_z):
                return False, "Nao referenciada (X/Z)"
        except Exception:
            # Se não conseguir ler, não bloqueia por isso
            pass

        # 3) Homing em andamento
        if bool(getattr(self, "_homing_busy", False)):
            return False, "Referenciando"

        # 4) Modo e execução
        try:
            task_mode = int(getattr(self.stat, "task_mode", -1))
        except Exception:
            task_mode = -1

        try:
            mode_auto = int(getattr(linuxcnc, "MODE_AUTO", 2))
        except Exception:
            mode_auto = 2

        try:
            interp_state = int(getattr(self.stat, "interp_state", -1))
        except Exception:
            interp_state = -1

        try:
            interp_idle = int(getattr(linuxcnc, "INTERP_IDLE", 1))
        except Exception:
            interp_idle = 1

        # Se estiver em AUTO e o interpretador NÃO estiver IDLE -> programa em execução (bloqueia)
        if task_mode == mode_auto and interp_state != -1 and interp_state != interp_idle:
            return False, "Programa em execucao"

        # IMPORTANTE:
        # Não bloqueie por interp_state fora do AUTO.
        # Em setups com M6 remap via MDI, o interp pode ficar em 2 mesmo após a troca.
        return True, ""


    def _tool_request(self, tool_num: int):
        """
        Aciona troca: envia 'Tn M6' via MDI em thread.
        Intertravado por _compute_tools_interlock().

        Fluxo ÚNICO:
            _tool_request -> _start_toolchange_thread -> _toolchange_worker
        """
        ok, reason = self._compute_tools_interlock()
        if not ok:
            print(f"[ICEQ] Troca bloqueada: {reason}")
            return

        # Evita reentrância
        if bool(getattr(self, "_toolchange_busy", False)):
            print("[ICEQ] Troca já em andamento, ignorando novo pedido.")
            return

        # Se já estiver com essa ferramenta, não faz nada
        try:
            self.stat.poll()
            cur = int(getattr(self.stat, "tool_in_spindle", 0))
            if int(cur) == int(tool_num):
                print(f"[ICEQ] Ferramenta T{tool_num} já ativa (tool_in_spindle). Ignorando.")
                return
        except Exception:
            pass

        # Marca início da troca (anti-flicker visual)
        self._toolchange_started_ts = time.time()

        # Lock imediato (UI)
        self._set_tool_buttons_enabled(False)


        # Força repaint imediato (evita “não apagou” visualmente)
        try:
            QtWidgets.QApplication.processEvents()
        except Exception:
            pass


        # Fluxo ESTÁVEL (igual ao MDI), em thread para não travar a GUI
        try:
            self._toolchange_busy = True
        except Exception:
            pass

        t = threading.Thread(
            target=self._toolchange_button_worker,
            args=(int(tool_num),),
            daemon=True
        )
        t.start()


    def _toolchange_button_worker(self, tool_num: int):
        """
        Executa Tn M6 usando o MESMO método do MDI (_run_mdi_command),
        porém em thread. NÃO atualiza UI direto aqui.
        """
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
            # Sempre libera estado (mesmo em erro) para não travar MDI e botões
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
        """
        Atualização mínima da UI das ferramentas:
        - label da ferramenta ativa
        - intertravamento (enable/disable)
        """
        try:
            # Ferramenta atual (LinuxCNC)
            active_tool = int(getattr(self.stat, "tool_in_spindle", 0))
        except Exception:
            active_tool = 0

        # Fallback: em setups com M6 remap, tool_in_spindle pode ficar 0
        if int(active_tool) <= 0:
            try:
                active_tool = int(getattr(self, "_tool_active_virtual", 0))
            except Exception:
                active_tool = 0

        # Atualiza label "Ferramenta Ativa: Tn"
        try:
            if active_tool != getattr(self, "_tool_active_last", None):
                self._tool_active_last = active_tool
                self._update_active_tool_label(active_tool)
        except Exception:
            pass

        # Realce do botão da ferramenta ativa (cor)
        try:
            self._update_active_tool_button_style(active_tool)
        except Exception:
            pass


    # ----------------------------------------------------------
    #   Atualiza painel de manutenção (LEDs / estados)
    # ----------------------------------------------------------
    def update_status_panel(self):
        """Chamado periodicamente pelo timer."""

        try:
            self.stat.poll()
        # ==========================================================
        # NOME DO ARQUIVO G-CODE CARREGADO (ABA PROGRAMA / EDITOR)
        # ==========================================================
            try:
                gcode_file = self.stat.file

                if not gcode_file:
                    display_name = "Nenhum Gcode carregado"
                else:
                    import os
                    display_name = os.path.basename(gcode_file)

                # Sempre em negrito
                text_html = f"<b>{display_name}</b>"

                if hasattr(self, "lbl_program_name"):
                    self.lbl_program_name.setText(text_html)

                if hasattr(self, "lbl_program_name2"):
                    self.lbl_program_name2.setText(text_html)

            except Exception as e:
                print(f"[ICEQ] Erro ao atualizar nome do G-code: {e}")


            # --- PREVIEW 2D: auto-reload quando o arquivo atual muda ---
            try:
                cur_file = str(getattr(self.stat, "file", "") or "")
                if cur_file and cur_file != self._preview2d_last_stat_file:
                    self._preview2d_last_stat_file = cur_file
                    self.preview2d.ensure_program_loaded(cur_file)
            except Exception:
                pass

            # --- NOME DO PROGRAMA CARREGADO (labels Programa / Editor) ---
            try:
                loaded_path = None

                # 1) Fonte oficial do LinuxCNC
                sf = str(getattr(self.stat, "file", "") or "")
                if sf:
                    loaded_path = sf

                # 2) Fallback interno (arquivo salvo/carregado recentemente)
                if not loaded_path:
                    loaded_path = getattr(self, "_program_loaded_path", None)

                self._update_program_name_labels(loaded_path)

            except Exception as e:
                print(f"[ICEQ] Erro ao atualizar nome do G-code (labels): {e}")


            # --- PREVIEW 2D: marcador de posição atual (X,Z) ---
            try:
                self.preview2d.tick_live(self.stat)
            except Exception:
                pass
            # --- PREVIEW 2D: destaque da linha atual do programa (estilo AXIS) ---
            try:
                self.preview2d.tick_program_line(self.stat)
            except Exception:
                pass

        except Exception as e:
            print(f"[ICEQ] update_status_panel: erro no stat.poll(): {e}")
            return

        estop   = bool(self.stat.estop)
        enabled = bool(self.stat.enabled)

        # Segurança: se JOG contínuo estiver ativo e a máquina não estiver pronta, para imediatamente
        try:
            if bool(getattr(self, "_jog_cont_active", False)):
                ok, _reason = self._jog_machine_ready()
                if not ok:
                    ax = str(getattr(self, "_jog_cont_axis", "") or "")
                    if ax in ("X", "Z"):
                        self._jog_continuous_release(ax)
        except Exception:
            pass


        # Debug no terminal para a gente enxergar o que o LinuxCNC está vendo
        # print(f"[ICEQ] tick  estop={estop}  enabled={enabled}")

        # ----- EMERGÊNCIA -----
        # estop == 1 -> emergência ATIVA -> emerg_ok = False (LED vermelho)
        emerg_ok = not estop
        self.set_led(self.led_maint_sig_emerg, emerg_ok)
        self.set_led(self.led_emerg, emerg_ok)
        self.lbl_maint_sig_emerg_state.setText("TRUE" if emerg_ok else "FALSE")

        # ----- MACHINE ON -----
        # Máquina só é "ON" se habilitada e sem E-STOP
        machine_on = enabled and not estop
        self.set_led(self.led_maint_sig_machine_on, machine_on)
        self.set_led(self.led_machine, machine_on)
        self.lbl_maint_sig_machine_on_state.setText("TRUE" if machine_on else "FALSE")

        # ------------------------------------------------------------
        # EARLY EXIT durante troca de ferramenta:
        # evita travar GUI com rotinas pesadas (spindle, highlight, progresso, etc.)
        # enquanto o M6 (interp 2) está ativo.
        # ------------------------------------------------------------
        try:
            if bool(getattr(self, "_toolchange_busy", False)) or bool(self._toolchange_hw_active()):
                self._update_tools_ui_tick()
                return
        except Exception:
            pass

        # ----- ESTADO DO PROGRAMA (RUN / PAUSE / STOP) -----
        mode   = self.stat.task_mode
        interp = self.stat.interp_state
        paused = bool(self.stat.paused)

        # ------------------------------------------------------------
        # Amarração industrial (botões do spindle travam durante ciclo)
        # ------------------------------------------------------------
        try:
            estop = bool(self.stat.estop)
            enabled = bool(self.stat.enabled)

            machine_ready = (not estop and enabled)
            auto_active = (mode == linuxcnc.MODE_AUTO and interp != linuxcnc.INTERP_IDLE)

            spindle_enable = (machine_ready and (not auto_active))
            coolant_enable = machine_ready

            if hasattr(self, "btn_spindle_rpm_plus"):
                self.btn_spindle_rpm_plus.setEnabled(spindle_enable)
            if hasattr(self, "btn_spindle_rpm_minus"):
                self.btn_spindle_rpm_minus.setEnabled(spindle_enable)
            if hasattr(self, "btn_spindle_cw"):
                self.btn_spindle_cw.setEnabled(spindle_enable)
            if hasattr(self, "btn_spindle_ccw"):
                self.btn_spindle_ccw.setEnabled(spindle_enable)
            if hasattr(self, "btn_spindle_stop"):
                self.btn_spindle_stop.setEnabled(spindle_enable)

            if hasattr(self, "btn_refri_button"):
                self.btn_refri_button.setEnabled(coolant_enable)

            # Atualiza o RPM mostrado na tela (sempre positivo)
            self._update_spindle_rpm_label()

        except Exception as e:
            print(f"[ICEQ] amarração spindle/coolant erro: {e}")

        # ------------------------------------------------------------
        # LEDs do rodapé + manutenção: spindle e coolant
        # Regra robusta:
        #   1) HAL (motion/halui/iocontrol) -> reflete AUTO e MDI
        #   2) STAT (self.stat.spindle[0])  -> fallback
        #   3) Estado interno (botões ICEQ)-> fallback final (manual)
        # Atualiza também dois padrões de widgets na MANUT:
        #   - novos: led_maint_sig_* + lbl_maint_sig_*_state
        #   - antigos: sig_* (se existirem)
        # ------------------------------------------------------------
        try:
            spindle_dir = 0   # +1=CW, -1=CCW, 0=STOP
            spindle_on  = False
            coolant_on  = False

            # 0) STAT (mais confiável em AUTO / Cycle Start)
            # Observação: em LinuxCNC 2.8, self.stat.spindle[0] pode ser dict-like, objeto com atributos,
            # ou tupla (enabled, direction, ...). Este bloco tenta cobrir todos.
            try:
                self.stat.poll()
                sp0 = None
                try:
                    sp0 = self.stat.spindle[0]
                except Exception:
                    sp0 = None

                if sp0 is not None:
                    # enabled / direction
                    if hasattr(sp0, "get"):
                        spindle_on = bool(sp0.get("enabled", False))
                        spindle_dir = int(sp0.get("direction", 0) or 0)
                    elif hasattr(sp0, "enabled"):
                        spindle_on = bool(getattr(sp0, "enabled", False))
                        spindle_dir = int(getattr(sp0, "direction", 0) or 0)
                    else:
                        # tuple/list fallback
                        try:
                            spindle_on = bool(sp0[0])
                            spindle_dir = int(sp0[1] or 0)
                        except Exception:
                            pass

                # coolant (flood/mist)
                coolant_on = bool(getattr(self.stat, "flood", False) or getattr(self.stat, "mist", False))
            except Exception:
                pass

            # -------------------- 1) HAL (AUTO + MDI) --------------------
            cw  = (self._hal_bit("halui.spindle.forward") or
                   self._hal_bit("halui.spindle.0.forward") or
                   self._hal_bit("motion.spindle-forward") or
                   self._hal_bit("iocontrol.0.spindle-forward") or
                   self._hal_bit("spindle.0.forward") or
                   False)

            ccw = (self._hal_bit("halui.spindle.reverse") or
                   self._hal_bit("halui.spindle.0.reverse") or
                   self._hal_bit("motion.spindle-reverse") or
                   self._hal_bit("iocontrol.0.spindle-reverse") or
                   self._hal_bit("spindle.0.reverse") or
                   False)

            on_hal = (self._hal_bit("motion.spindle-on") or
                      self._hal_bit("iocontrol.0.spindle-on") or
                      self._hal_bit("spindle.0.on") or
                      False)

            if not on_hal:
                on_hal = bool(cw or ccw)

            if on_hal and cw and not ccw:
                spindle_on = True
                spindle_dir = 1
            elif on_hal and ccw and not cw:
                spindle_on = True
                spindle_dir = -1
            elif on_hal and (cw or ccw):
                spindle_on = True
                spindle_dir = 1 if cw else (-1 if ccw else 0)

            # -------------------- 2) STAT (fallback) --------------------
            if not spindle_on:
                try:
                    sp = self.stat.spindle[0]
                    sp_enabled = bool(getattr(sp, "enabled", False))
                    sp_dir = int(getattr(sp, "direction", 0))
                    sp_speed = float(getattr(sp, "speed", 0.0))

                    if sp_dir == 0 and abs(sp_speed) > 0.1:
                        sp_dir = 1 if sp_speed > 0 else -1

                    if (sp_enabled and sp_dir != 0) or (abs(sp_speed) > 0.1 and sp_dir != 0):
                        spindle_on = True
                        spindle_dir = sp_dir
                except Exception:
                    pass

            # -------------------- 3) Estado interno (manual ICEQ) --------------------
            if not spindle_on:
                try:
                    rpm_sp = int(abs(getattr(self, "_spindle_rpm_setpoint", 0)))
                except Exception:
                    rpm_sp = 0

                try:
                    dir_int = int(getattr(self, "_spindle_dir", 0))  # 1, -1, 0
                except Exception:
                    dir_int = 0

                if dir_int != 0 and rpm_sp > 0:
                    spindle_on = True
                    spindle_dir = dir_int

            # -------------------- Coolant --------------------
            coolant_on = False
            try:
                coolant_on = bool(self._get_coolant_on_safe())
            except Exception:
                coolant_on = False

            # -------------------- Rodapé --------------------
            if hasattr(self, "led_spindle"):
                self.set_led(self.led_spindle, spindle_on)

            if hasattr(self, "led_coolant"):
                self.set_led(self.led_coolant, coolant_on)

            # -------------------- MANUT (novo padrão) --------------------
            if hasattr(self, "led_maint_sig_spindle_cw"):
                self.set_led(self.led_maint_sig_spindle_cw, spindle_dir > 0)
            if hasattr(self, "led_maint_sig_spindle_ccw"):
                self.set_led(self.led_maint_sig_spindle_ccw, spindle_dir < 0)
            if hasattr(self, "led_maint_sig_spindle_stop"):
                self.set_led(self.led_maint_sig_spindle_stop, not spindle_on)

            self._set_state_label("lbl_maint_sig_spindle_cw_state",   spindle_dir > 0)
            self._set_state_label("lbl_maint_sig_spindle_ccw_state",  spindle_dir < 0)
            self._set_state_label("lbl_maint_sig_spindle_stop_state", not spindle_on)

            if hasattr(self, "led_maint_sig_coolant"):
                self.set_led(self.led_maint_sig_coolant, coolant_on)
            self._set_state_label("lbl_maint_sig_coolant_state", coolant_on)

            # -------------------- MANUT (padrão antigo sig_*) --------------------
            if hasattr(self, "sig_spindle_cw"):
                self.set_led(self.sig_spindle_cw, spindle_dir > 0)
            if hasattr(self, "sig_spindle_ccw"):
                self.set_led(self.sig_spindle_ccw, spindle_dir < 0)
            if hasattr(self, "sig_spindle_stop"):
                self.set_led(self.sig_spindle_stop, not spindle_on)

            if hasattr(self, "sig_coolant"):
                self.set_led(self.sig_coolant, coolant_on)

        except Exception as e:
            print(f"[ICEQ] LEDs spindle/coolant erro: {e}")

        # ------------------------------------------------------------
        # MANUT - Diagnóstico RPM / Spindle Override
        # ------------------------------------------------------------
        try:
            rpm_real = self._get_spindle_rpm_safe()

            # Fonte do RPM (encoder ou sim)
            rpm_source = "SIM"
            try:
                import hal
                if hasattr(hal, "get_value"):

                    v = hal.get_value("spindle.0.speed-in")
                    if v is not None and abs(float(v)) > 0.1:
                        rpm_source = "ENCODER"
            except Exception:
                pass

            # Atualiza labels da aba MANUT (se existirem)
            self._set_label_if_exists(
                "lbl_maint_spindle_rpm",
                f"{rpm_real:.0f} RPM"
            )

            self._set_label_if_exists(
                "lbl_maint_spindle_rpm_src",
                rpm_source
            )

            self._set_label_if_exists(
                "lbl_maint_spindle_override",
                f"{int(getattr(self, '_spindle_ovr_pct', 100))}%"
            )

        except Exception as e:
            print(f"[ICEQ] MANUT RPM erro: {e}")


        # ------------------------------------------------------------
        # ESTADOS (coluna "ESTADO" na aba MANUT) - fonte ÚNICA (STAT/HAL robusto)
        # ------------------------------------------------------------
        try:
            # Usa o mesmo spindle_on/spindle_dir já calculado acima (robusto)
            v_cw   = bool(spindle_on and spindle_dir > 0)
            v_ccw  = bool(spindle_on and spindle_dir < 0)
            v_stop = bool(not spindle_on)

            v_col = bool(coolant_on)

            # ------------------ SPINDLE (4 sinais adicionais na MANUT) ------------------
            # 1) Spindle ON (feedback de "enable" do componente spindle)
            #    Preferência: spindle.0.on (HAL) -> fallback: spindle_on robusto
            v_spindle_on = bool(
                self._hal_bit("spindle.0.on") or
                self._hal_bit("halui.spindle.0.is-on") or
                spindle_on
            )

            # Lê RPM comandado (no seu HAL, spindle.0.speed-out é RPM comandado)
            cmd_rpm = 0.0
            try:
                cmd_rpm = abs(float(self._hal_float("spindle.0.speed-out") or 0.0))
                if cmd_rpm <= 0.0:
                    cmd_rpm = abs(float(self._hal_float("spindle.0.speed-out-abs") or 0.0))
            except Exception:
                cmd_rpm = 0.0

            # 2) Spindle AT SPEED (ENCODER FEEDBACK)
            #    Regra: AT SPEED = spindle ligado + cmd_rpm válido + fb_rpm válido + erro dentro da tolerância,
            #    confirmado por N leituras consecutivas (debounce).
            sp_at_speed = False
            try:
                # Comando em RPM (no seu print: spindle.0.speed-out = rpm comandado)
                cmd_rpm = abs(float(self._hal_float("spindle.0.speed-out") or 0.0))
                if cmd_rpm <= 0.0:
                    cmd_rpm = abs(float(self._hal_float("spindle.0.speed-out-abs") or 0.0))

                # Feedback do encoder deve chegar em spindle.0.speed-in (RPS)
                fb_rps = abs(float(self._hal_float("spindle.0.speed-in") or 0.0))
                fb_rpm = fb_rps * 60.0

                if v_spindle_on and cmd_rpm > 0.0 and fb_rpm > 0.0:
                    tol = max(float(self._sp_at_speed_tol_abs_rpm), float(self._sp_at_speed_tol_pct) * cmd_rpm)
                    if abs(cmd_rpm - fb_rpm) <= tol:
                        sp_at_speed = True
            except Exception:
                sp_at_speed = False

            # Debounce: exige N leituras consecutivas antes de afirmar TRUE
            if sp_at_speed:
                self._sp_at_speed_cnt = min(self._sp_at_speed_cnt + 1, 9999)
            else:
                self._sp_at_speed_cnt = 0

            v_spindle_at_speed = bool(self._sp_at_speed_cnt >= int(self._sp_at_speed_required_hits))
            self._sp_at_speed_last = v_spindle_at_speed

            # 3) RPMFB ACTIVE (tem feedback “vivo” de velocidade)
            rpmfb_active = False
            try:
                act_rps = abs(float(self._hal_float("spindle.0.speed-in") or 0.0))
                rpmfb_active = (act_rps > 0.1)
            except Exception:
                rpmfb_active = False

            v_rpmfb_active = bool(rpmfb_active)

            # 4) Spindle FAULT (no seu setup existe spindle.0.amp-fault-in)
            v_spindle_fault = bool(
                self._hal_bit("spindle.0.amp-fault-in") or
                self._hal_bit("spindle.0.orient-fault") or
                self._hal_bit("motion.spindle-fault") or
                self._hal_bit("iocontrol.0.spindle-fault") or
                False
            )

            # Atualiza os 4 LEDs adicionais da MANUT
            if hasattr(self, "led_maint_sig_spindle_on"):
                self.set_led(self.led_maint_sig_spindle_on, v_spindle_on)
            if hasattr(self, "led_maint_sig_spindle_at_speed"):
                self.set_led(self.led_maint_sig_spindle_at_speed, v_spindle_at_speed)
            if hasattr(self, "led_maint_sig_rpm_fb_active"):
                self.set_led(self.led_maint_sig_rpm_fb_active, v_rpmfb_active)
            if hasattr(self, "led_maint_sig_spindle_fault"):
                self.set_led(self.led_maint_sig_spindle_fault, v_spindle_fault)

            # Labels (TRUE/FALSE) - 4 LEDs acima
            self._set_state_label("lbl_maint_sig_spindle_on_state", v_spindle_on)
            self._set_state_label("lbl_maint_sig_spindle_at_speed_state", v_spindle_at_speed)
            self._set_state_label("lbl_maint_sig_rpm_fb_active_state", v_rpmfb_active)
            self._set_state_label("lbl_maint_sig_spindle_fault_state", v_spindle_fault)

            # Labels (TRUE/FALSE) - CW/CCW/STOP/COOLANT
            self._set_state_label("lbl_maint_sig_spindle_cw_state",   v_cw)
            self._set_state_label("lbl_maint_sig_spindle_ccw_state",  v_ccw)
            self._set_state_label("lbl_maint_sig_spindle_stop_state", v_stop)
            self._set_state_label("lbl_maint_sig_coolant_state",      v_col)

            # LEDs (mesmo valor dos labels) - CW/CCW/STOP/COOLANT
            if hasattr(self, "led_maint_sig_spindle_cw"):
                self.set_led(self.led_maint_sig_spindle_cw, v_cw)
            if hasattr(self, "led_maint_sig_spindle_ccw"):
                self.set_led(self.led_maint_sig_spindle_ccw, v_ccw)
            if hasattr(self, "led_maint_sig_spindle_stop"):
                self.set_led(self.led_maint_sig_spindle_stop, v_stop)
            if hasattr(self, "led_maint_sig_coolant"):
                self.set_led(self.led_maint_sig_coolant, v_col)

        except Exception as e:
            # Evita flood a cada 200ms
            msg = f"[ICEQ] estados MANUT erro: {e}"
            if getattr(self, "_dbg_last_status", "") != msg:
                self._dbg_last_status = msg
                print(msg)

        # ------------------------------------------------------------
        # MONITOR: TORRE / TOOLCHANGE
        # ------------------------------------------------------------
        try:
            # TOOLCHANGE ativo (derivado do sistema)
            v_tc_active = bool(getattr(self, "_toolchange_busy", False)) or bool(self._toolchange_hw_active())

            # TOOL ativo (tool_in_spindle com fallback)
            try:
                tool_stat = int(getattr(self.stat, "tool_in_spindle", 0) or 0)
            except Exception:
                tool_stat = 0
            tool_fallback = int(getattr(self, "_tool_active_virtual", 0) or 0)
            tool_now = tool_stat if tool_stat > 0 else tool_fallback
            v_tool_active = bool(tool_now > 0)

            # Solenoide: primeiro tenta pelo PIN do .ui; se não existir, fallback DO6
            v_sol = self._hal_out_from_label("lbl_maint_sig_turret_solenoid_pin")
            if v_sol is None:
                v_sol = bool(self._hal_bit_multi(["motion.digital-out-06", "motion.digital-out-6"]))

            # Motor CW/CCW: tenta pelos PINs do .ui (OUTn) ou nome HAL direto
            v_cw = self._hal_out_from_label("lbl_maint_sig_turret_cw_pin")
            v_ccw = self._hal_out_from_label("lbl_maint_sig_turret_ccw_pin")

            # Default seguro
            v_cw = bool(v_cw) if v_cw is not None else False
            v_ccw = bool(v_ccw) if v_ccw is not None else False
            v_sol = bool(v_sol)

            # LEDs
            if hasattr(self, "led_maint_sig_toolchange_active"):
                self.set_led(self.led_maint_sig_toolchange_active, v_tc_active)
            if hasattr(self, "led_maint_sig_tool_active"):
                self.set_led(self.led_maint_sig_tool_active, v_tool_active)
            if hasattr(self, "led_maint_sig_turret_solenoid"):
                self.set_led(self.led_maint_sig_turret_solenoid, v_sol)
            if hasattr(self, "led_maint_sig_turret_cw"):
                self.set_led(self.led_maint_sig_turret_cw, v_cw)
            if hasattr(self, "led_maint_sig_turret_ccw"):
                self.set_led(self.led_maint_sig_turret_ccw, v_ccw)

            # STATE labels (TRUE/FALSE)
            self._set_state_label("lbl_maint_sig_toolchange_active_state", v_tc_active)
            self._set_state_label("lbl_maint_sig_tool_active_state", v_tool_active)
            self._set_state_label("lbl_maint_sig_turret_solenoid_state", v_sol)
            self._set_state_label("lbl_maint_sig_turret_cw_state", v_cw)
            self._set_state_label("lbl_maint_sig_turret_ccw_state", v_ccw)

        except Exception as e:
            print(f"[ICEQ] MONITOR torre/toolchange erro: {e}")


        # -----------------------------
        # Ferramentas: UI tick (estado + label + interlock visual)
        # -----------------------------
        self._update_tools_ui_tick()

        # Ferramentas: intertravamento VISUAL (somente quando NÃO está em troca)
        try:
            # Anti-flicker: respeita janela mínima após clique
            ts = float(getattr(self, "_toolchange_started_ts", 0.0) or 0.0)
            min_s = float(getattr(self, "_toolchange_lock_min_s", 0.0) or 0.0)

            if ts > 0.0 and min_s > 0.0 and (time.time() - ts) < min_s:
                # Durante a janela mínima, NÃO reabilita
                pass
            else:
                if not bool(getattr(self, "_toolchange_busy", False)) and not bool(self._toolchange_hw_active()):
                    ok, reason = self._compute_tools_interlock()
                    self._tool_buttons_locked_reason = reason
                    self._set_tool_buttons_enabled(bool(ok))
        except Exception:
            self._set_tool_buttons_enabled(False)


        # ----- RPM do spindle (rodapé) -----
        try:
            # Detecta se spindle está realmente ON no LinuxCNC (fonte real)
            sp_on = False
            sp_speed_base = 0.0
            try:
                sp = self.stat.spindle[0]
                sp_enabled = bool(sp.get("enabled", 0))
                sp_dir = int(sp.get("direction", 0))
                sp_speed_base = abs(float(sp.get("speed", 0.0)))
                sp_on = (sp_enabled and sp_dir != 0 and sp_speed_base > 0.1)
            except Exception:
                sp_on = False
                sp_speed_base = 0.0

            # Override atual (0..120)
            ovr = 100
            try:
                ovr = int(getattr(self, "_spindle_ovr_pct", 100))
            except Exception:
                ovr = 100
            ovr = max(0, min(120, ovr))

            if sp_on:
                rpm_eff = sp_speed_base * (float(ovr) / 100.0)
                self._set_label_if_exists("lbl_spindle_rpm", f"{rpm_eff:.0f} RPM")
                self._update_spindle_rpm_bar(rpm_eff)
            else:
                self._set_label_if_exists("lbl_spindle_rpm", "0 RPM")
                self._update_spindle_rpm_bar(0.0)


        except Exception as e:
            print(f"[ICEQ] erro RPM: {e}")

        # -------------------------------------------------------------
        # PROGRESSO DO PROGRAMA (prg_cycle_top) - % por linha executada
        # -------------------------------------------------------------
        try:
            mode   = self.stat.task_mode
            interp = self.stat.interp_state

            # só atualiza enquanto o interpretador estiver "rodando" (não IDLE)
            if mode == linuxcnc.MODE_AUTO and interp != linuxcnc.INTERP_IDLE:
                if self._gcode_total_lines > 0:
                    cur_line = int(getattr(self.stat, "current_line", 0))

                    # current_line costuma ser 1-based; garante limites
                    if cur_line < 0:
                        cur_line = 0
                    if cur_line > self._gcode_total_lines:
                        cur_line = self._gcode_total_lines

                    pct = int((cur_line * 100) / float(self._gcode_total_lines))
                    if pct < 0:
                        pct = 0
                    if pct > 100:
                        pct = 100

                    self._last_progress_pct = pct

                    if hasattr(self, "prg_cycle_top"):
                        self.prg_cycle_top.setValue(pct)
                        self.prg_cycle_top.setFormat(f"{pct}%")
            else:
                # programa terminou (IDLE) -> fecha em 100% e congela
                if hasattr(self, "prg_cycle_top"):
                    self.prg_cycle_top.setValue(100)
                    self.prg_cycle_top.setFormat("100%")
                self._last_progress_pct = 100

            # quando termina/idle: não mexe (fica congelado no último valor)
            # (igual você pediu para o tempo do ciclo)
        except Exception as e:
            print(f"[ICEQ] erro progresso: {e}")

        # Debug para entender o que o LinuxCNC está vendo
        cur = (int(mode), int(interp), bool(paused))
        if cur != self._dbg_last_status:
            self._dbg_last_status = cur
            print(f"[ICEQ] status: mode={mode} interp={interp} paused={paused}")

        # Se não estiver em AUTO, consideramos programa parado
        if mode != linuxcnc.MODE_AUTO:
            program_running = False
            program_paused  = False
        else:
            # Em AUTO:
            #  - paused True                      -> PAUSADO
            #  - paused False e interp != IDLE    -> RODANDO
            program_paused  = paused
            program_running = (not paused and interp != linuxcnc.INTERP_IDLE)

        # LEDs de START / PAUSE na aba MANUT
        self.set_led(self.led_maint_sig_start, program_running)
        self.set_led(self.led_maint_sig_pause, program_paused)

        # Labels de estado na MANUT (se estiverem criados)
        if hasattr(self, "lbl_maint_sig_start_state"):
            self.lbl_maint_sig_start_state.setText(
                "TRUE" if program_running else "FALSE"
            )
        if hasattr(self, "lbl_maint_sig_pause_state"):
            self.lbl_maint_sig_pause_state.setText(
                "TRUE" if program_paused else "FALSE"
            )

        # LED PROGRAMA no rodapé (tri-cor)
        if program_running:
            # VERDE = rodando
            color = "rgb(0, 255, 0)"
        elif program_paused:
            # AMARELO = pausado
            color = "rgb(255, 255, 0)"
        else:
            # VERMELHO = parado / idle / abortado
            color = "rgb(255, 0, 0)"

        self.led_program.setStyleSheet(
            f"background-color: {color}; border: 1px solid black;"
        )

        # ----------------------------------------------------
        # CABEÇALHO: X / Z / VEL  (com legenda + formatação)
        # ----------------------------------------------------
        try:
            lu = float(getattr(self.stat, "linear_units", 1.0))

            # Converte "unidade do stat" -> mm (robusto para lu=0.03937 ou lu=25.4)
            def to_mm(v):
                try:
                    v = float(v)
                    if lu < 0.999:      # ex.: 0.03937 (inch/mm) -> divide para obter mm
                        return v / lu
                    elif lu > 1.001:    # ex.: 25.4 (mm/inch) -> multiplica
                        return v * lu
                    else:               # lu ~ 1.0
                        return v
                except Exception:
                    return 0.0

            # posição (prefere actual_position; fallback position)
            pos = getattr(self.stat, "actual_position", None)
            if pos is None:
                pos = getattr(self.stat, "position", None)

            if pos is not None:
                # Ajuste estes índices se seu mapeamento for diferente
                x_mm = to_mm(pos[0])   # X
                z_mm = to_mm(pos[2])   # Z

                # TORNO: exibe X em DIÂMETRO (se o stat vier em raio)
                x_disp = x_mm * 2.0

                # AJUSTE os nomes abaixo para os seus widgets do cabeçalho
                # (mantém legenda + 3 casas decimais)
                if hasattr(self, "lbl_hdr_x"):
                    self.lbl_hdr_x.setText(f"X: {x_disp:.3f}")
                if hasattr(self, "lbl_hdr_z"):
                    self.lbl_hdr_z.setText(f"Z: {z_mm:.3f}")

            # velocidade atual (current_vel é unidade/seg) -> mm/s
            v = getattr(self.stat, "current_vel", None)
            if v is not None:
                v_mm_min = to_mm(v) * 60.0

                # legenda + 2 casas decimais + unidade
                if hasattr(self, "lbl_hdr_vel"):
                    self.lbl_hdr_vel.setText(f"VEL: {v_mm_min:.2f} mm/min")

        except Exception as e:
            print(f"[ICEQ] cabecalho X/Z/VEL: erro: {e}")



        # =========================================================
        # RPM máximo configurado do spindle (INI)
        # =========================================================
        try:
            import os
            import configparser
            import re

            # garante estilo (negrito + fonte 10) independente do .ui
            try:
                self.lbl_rpm_max.setStyleSheet("font-size: 10pt; font-weight: bold;")
            except Exception:
                pass

            ini_path = (
                os.environ.get("INI_FILE_NAME")
                or os.environ.get("EMC2_INI_FILE_NAME")
                or os.environ.get("LINUXCNC_INI")  # fallback comum em alguns setups
                or ""
            )

            rpm_max_val = None

            def _parse_ini_number(raw):
                """
                Extrai o primeiro número do texto (aceita '1100.0 ; coment', '1100,0', etc.)
                """
                if raw is None:
                    return None
                s = str(raw).strip()
                # remove comentários inline típicos
                s = s.split(";")[0].split("#")[0].strip()
                # pega o primeiro número encontrado
                m = re.search(r"[-+]?\d+(?:[.,]\d+)?", s)
                if not m:
                    return None
                num = m.group(0).replace(",", ".")
                try:
                    return float(num)
                except Exception:
                    return None

            if ini_path:
                # Evita problemas de interpolação (%) e aceita comentários inline
                cfg = configparser.RawConfigParser(
                    inline_comment_prefixes=(";", "#"),
                    strict=False
                )
                loaded = cfg.read(ini_path)

                if loaded:
                    # nomes de seção mais comuns
                    sec_found = None
                    for sec in ("SPINDLE_0", "spindle_0", "SPINDLE", "spindle"):
                        if cfg.has_section(sec):
                            sec_found = sec
                            break

                    if sec_found:
                        # tenta chaves mais comuns (em ordem de preferência)
                        for key in ("MAX_RPM", "MAX_SPEED", "MAX_VELOCITY", "MAX_OUTPUT"):
                            v = cfg.get(sec_found, key, fallback=None)
                            fv = _parse_ini_number(v)
                            if fv is not None and fv > 0:
                                rpm_max_val = int(round(fv))
                                break

            if rpm_max_val is not None and rpm_max_val > 0:
                self._spindle_rpm_max_val = int(rpm_max_val)
                self.lbl_rpm_max.setText(f"{rpm_max_val} rpm/max")
            else:
                self._spindle_rpm_max_val = 0
                self.lbl_rpm_max.setText("RPM máx indef.")


        except Exception:
            self._spindle_rpm_max_val = 0
            self.lbl_rpm_max.setText("RPM máx indef.")


        # =========================================================
        # Velocidade máxima configurada (INI) — mm/min (G0/rápido)
        # =========================================================
        try:
            vmax_mm_min = float(self._get_traj_max_mm_min_safe() or 0.0)

            if hasattr(self, "lbl_vel_machine_oper"):
                # garante padrão visual (fonte 10 + negrito)
                try:
                    self.lbl_vel_machine_oper.setStyleSheet("font-size:10pt; font-weight:600;")
                except Exception:
                    pass

                if vmax_mm_min > 0.0:
                    self.lbl_vel_machine_oper.setText(f"{int(round(vmax_mm_min))} mm/min máx")
                else:
                    self.lbl_vel_machine_oper.setText("VEL máx indef.")
        except Exception:
            try:
                if hasattr(self, "lbl_vel_machine_oper"):
                    self.lbl_vel_machine_oper.setText("VEL máx indef.")
            except Exception:
                pass


        # =========================================================
        # Velocidade ATUAL configurada (override) — mm/min
        # (base: VEL máx do INI × % do slider/spinbox)
        # =========================================================
        try:
            if hasattr(self, "lbl_vel_machine"):

                # padrão visual (fonte 10 + negrito)
                try:
                    self.lbl_vel_machine.setStyleSheet("font-size:10pt; font-weight:600;")
                except Exception:
                    pass

                vmax_mm_min = float(self._get_traj_max_mm_min_safe() or 0.0)

                pct = None
                try:
                    pct = int(getattr(self, "_machine_ovr_pct", None))
                except Exception:
                    pct = None

                if pct is None and hasattr(self, "sld_vel_machine_oper"):
                    try:
                        pct = int(self.sld_vel_machine_oper.value())
                    except Exception:
                        pct = None

                if pct is None and hasattr(self, "spn_vel_machine_oper"):
                    try:
                        pct = int(self.spn_vel_machine_oper.value())
                    except Exception:
                        pct = None

                if pct is not None:
                    pct = int(max(0, min(120, pct)))

                vcur_mm_min = None


                # Preferir o override REAL aplicado pelo LinuxCNC (STAT),
                # para bater com o que o sistema efetivamente usa no G0/G1.
                scale = None
                try:
                    rr = getattr(self.stat, "rapidrate", None)   # 0.0..1.2
                    fr = getattr(self.stat, "feedrate", None)    # 0.0..1.2

                    # Para G0, rapidrate é o mais representativo
                    if isinstance(rr, (int, float)) and rr is not None:
                        scale = float(rr)
                    elif isinstance(fr, (int, float)) and fr is not None:
                        scale = float(fr)
                except Exception:
                    scale = None

                # fallback: usa o pct do widget
                if scale is None and pct is not None:
                    scale = float(pct) / 100.0

                if vmax_mm_min > 0.0 and scale is not None:
                    vcur_mm_min = vmax_mm_min * max(0.0, scale)


                if vcur_mm_min is not None:
                    self.lbl_vel_machine.setText(f"{vcur_mm_min:.2f} mm/min")
                else:
                    self.lbl_vel_machine.setText("VEL atual indef.")

        except Exception:
            try:
                if hasattr(self, "lbl_vel_machine"):
                    self.lbl_vel_machine.setText("VEL atual indef.")
            except Exception:
                pass



        # ----------------------------------------------------
        # DESTAQUE DA LINHA ATUAL DO G-CODE (Editor + Viewer)
        # ----------------------------------------------------
        try:
            mode   = self.stat.task_mode
            interp = int(self.stat.interp_state)
            paused = bool(self.stat.paused)

            if mode == linuxcnc.MODE_AUTO and interp != linuxcnc.INTERP_IDLE:
                cl = int(self.stat.current_line)

                # ------------------------------------------------
                # LÓGICA CORRETA:
                # - Enquanto a linha AINDA está em execução,
                #   o interpreter costuma apontar para a próxima.
                # - Se estiver WAITING ou PAUSED, mantém a linha anterior.
                # - Caso contrário, usa a linha reportada.
                # ------------------------------------------------
                if paused:
                    current_line = max(0, cl - 1)
                elif interp == linuxcnc.INTERP_WAITING:
                    current_line = max(0, cl - 1)
                else:
                    current_line = max(0, cl)

                # Editor
                if hasattr(self, "txt_editor"):
                    self._highlight_gcode_line(self.txt_editor, current_line)

                # Visualização G-code
                if hasattr(self, "txt_gcode_view"):
                    self._highlight_gcode_line(self.txt_gcode_view, current_line)

            else:
                # Programa parado / abortado → limpa destaque
                if hasattr(self, "txt_editor"):
                    self._clear_gcode_highlight(self.txt_editor)

                if hasattr(self, "txt_gcode_view"):
                    self._clear_gcode_highlight(self.txt_gcode_view)

        except Exception as e:
            print(f"[ICEQ] erro highlight gcode: {e}")

        # ----------------------------------------------------
        # CICLO: tempo + progresso (barra superior)
        # ----------------------------------------------------
        try:
            mode = self.stat.task_mode
            interp = self.stat.interp_state
            paused = bool(getattr(self.stat, "paused", False))

            program_active = (mode == linuxcnc.MODE_AUTO and interp != linuxcnc.INTERP_IDLE)
            program_running = (program_active and not paused)

            # Detecta INÍCIO de ciclo (primeira vez que entra rodando)
            if program_running and not self._cycle_running:
                self._cycle_running = True
                self._cycle_start_ts = time.monotonic()
                self._cycle_last_elapsed = 0.0

            # Atualiza tempo enquanto estiver ativo (conta inclusive pausas, mas congela ao finalizar)
            if self._cycle_running and self._cycle_start_ts is not None:
                elapsed_now = time.monotonic() - self._cycle_start_ts
            else:
                elapsed_now = self._cycle_last_elapsed

            # Se programa terminou/abortou (voltou para IDLE), congela o último tempo
            if self._cycle_running and not program_active:
                self._cycle_running = False
                self._cycle_last_elapsed = elapsed_now

            # Atualiza label do tempo (rodando ou congelado)
            if hasattr(self, "lbl_cycle_time_top"):
                if self._cycle_running:
                    self.lbl_cycle_time_top.setText(self._format_hms(elapsed_now))
                else:
                    self.lbl_cycle_time_top.setText(self._format_hms(self._cycle_last_elapsed))

            # Progresso do G-code (0..100%)
            if hasattr(self, "prg_cycle_top"):
                if program_active and self._gcode_total_lines and self._gcode_total_lines > 0:
                    cur = int(getattr(self.stat, "current_line", 0))
                    # current_line pode apontar para "próxima linha"; para progresso, usar cur (não -1)
                    pct = (float(cur) / float(self._gcode_total_lines)) * 100.0
                    if pct < 0.0:
                        pct = 0.0
                    if pct > 100.0:
                        pct = 100.0
                    self.prg_cycle_top.setValue(int(pct))
                else:
                    # Se não tem programa ativo, não zera automaticamente (fica no último ciclo),
                    # mas se não tem arquivo carregado, mostra 0.
                    if not self._gcode_total_lines:
                        self.prg_cycle_top.setValue(0)

        except Exception as e:
            print(f"[ICEQ] ciclo tempo/progresso: erro: {e}")

        # ----------------------------------------------------
        # INTERTRAVAMENTO VISUAL (botões principais)
        # ----------------------------------------------------
        try:
            self._update_interlock_visuals()
        except Exception as e:
            print(f"[ICEQ] intertravamento visual: erro: {e}")


    # ----------------------------------------------------
    # GRIFO DA LINHA ATUAL (ABA PROGRAMA) - via Highlighter
    # ----------------------------------------------------
    def _get_exec_gcode_line(self):
        """
        Retorna a melhor estimativa da linha REALMENTE em execução.

        Preferência:
        1) stat.motion_line (se existir) -> costuma representar a linha em movimento.
        2) stat.current_line (fallback)  -> pode ser "próxima linha", então corrigimos por estado.
        """
        try:
            self.stat.poll()
        except Exception:
            return None

        # 1) Preferir motion_line se existir na sua versão
        if hasattr(self.stat, "motion_line"):
            try:
                ml = int(self.stat.motion_line)
                if ml >= 0:
                    return ml
            except Exception:
                pass

        # 2) Fallback: current_line
        if not hasattr(self.stat, "current_line"):
            return None

        try:
            cl = int(self.stat.current_line)
        except Exception:
            return None

        if cl < 0:
            return None

        # Alguns LinuxCNC apontam current_line como "próxima linha" durante execução.
        # A correção NÃO pode ser fixa. Vamos usar interp_state/paused.
        try:
            interp = int(self.stat.interp_state)
            paused = bool(self.stat.paused)
        except Exception:
            interp = None
            paused = False

        # Regra prática:
        # - Se está PAUSADO: normalmente a linha reportada já é a linha onde parou (não desconta).
        # - Se está RODANDO (não pausado): muitas vezes current_line aponta para a próxima -> desconta 1.
        # Isso elimina o erro típico de "grifando a linha de baixo".
        if not paused:
            cl = cl - 1

        if cl < 0:
            cl = 0

        return cl

    def _highlight_gcode_line(self, widget, line_index):
        """
        Destaca visualmente a linha 'line_index' em um QTextEdit/QPlainTextEdit.
        """
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

        except Exception as e:
            print(f"[ICEQ] erro ao destacar linha: {e}")


    def _clear_gcode_highlight(self, widget):
        try:
            if widget is None:
                return
            cursor = widget.textCursor()
            cursor.clearSelection()
            widget.setTextCursor(cursor)
        except Exception:
            pass

    # ---------------------------------------------------------
    # Funcao de abrir programa para os dois botoes de abrir
    # ---------------------------------------------------------
    def open_program(self):
        """
        Abre uma janela para escolher G-code e carrega no LinuxCNC.
        """

        # ---- janela para selecionar o arquivo ----
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "Abrir Programa G-code",
            "/home/iceq/linuxcnc/configs/nc_files",  # pasta inicial
            "G-code (*.ngc *.nc *.tap *.gcode);;Todos (*.*)"
        )

        # se o usuario cancelar, sai da funcao
        if not filename:
            print("[ICEQ] open_program: usuario cancelou.")
            return

        print(f"[ICEQ] Abrindo arquivo: {filename}")

        # ---- abre o programa no LinuxCNC ----
        try:
            self.cmd.program_open(filename)
        except Exception as e:
            print(f"[ICEQ] Erro ao abrir programa no LinuxCNC: {e}")
            return

        # ---- le o conteudo do arquivo ----
        try:
            with open(filename, 'r') as f:
                conteudo = f.read()
                # ----------------- prepara progresso (total de linhas) -----------------
                try:
                    linhas = conteudo.splitlines()
                    self._gcode_total_lines = max(1, len(linhas))
                    self._last_progress_pct = 0

                    if hasattr(self, "prg_cycle_top"):
                        self.prg_cycle_top.setValue(0)
                        self.prg_cycle_top.setFormat("0%")
                except Exception as e:
                    print(f"[ICEQ] erro preparando progresso: {e}")
                    self._gcode_total_lines = 0
                    self._last_progress_pct = 0
        except Exception as e:
            print(f"[ICEQ] Erro ao ler arquivo '{filename}': {e}")
            return

            # Guarda path e calcula um "total de linhas" para estimar o progresso
            self._gcode_loaded_path = filename
            self._gcode_total_lines = self._count_gcode_lines(conteudo)

            # Zera barra ao carregar novo programa (o tempo do último ciclo fica congelado)
            if hasattr(self, "prg_cycle_top"):
                self.prg_cycle_top.setValue(0)

        # ---- aba PROGRAMA (visualizacao) ----
        if hasattr(self, "txt_gcode_view"):
            try:
                self.txt_gcode_view.setPlainText(conteudo)
            except Exception as e:
                print(f"[ICEQ] Erro ao carregar em txt_gcode_view: {e}")

        # ---- aba EDITOR (edicao) ----
        if hasattr(self, "txt_editor"):
            try:
                self.txt_editor.setPlainText(conteudo)
            except Exception as e:
                print(f"[ICEQ] Erro ao carregar em txt_editor: {e}")

    def _program_open_path(self, file_path):
        """
        Carrega um programa no LinuxCNC (equivalente ao botão Abrir).
        Mantém fallback interno para UI (labels) caso stat.file demore/venha vazio.
        """
        try:
            if not file_path:
                return

            # guarda fallback imediatamente
            self._program_loaded_path = str(file_path)

            # carrega no LinuxCNC
            with self._cmd_lock:
                self.cmd.program_open(str(file_path))
                try:
                    self.cmd.wait_complete()
                except Exception:
                    pass

            # atualiza labels já
            self._update_program_name_labels(str(file_path))
            print(f"[ICEQ] Programa carregado: {file_path}")

        except Exception as e:
            print(f"[ICEQ][ERRO] Falha ao carregar programa: {e}")


    def _editor_save(self):
        """
        Salva o arquivo atualmente em edição.
        - Se já existir caminho, salva direto.
        - Se não existir, abre 'Salvar como'.
        """
        try:
            text = self.txt_editor.toPlainText()

            if not text.strip():
                print("[ICEQ] Editor vazio, nada para salvar.")
                return

            # =========================================================
            # CENÁRIO A — Arquivo já possui caminho
            # =========================================================
            if self._editor_current_file:
                with open(self._editor_current_file, "w", encoding="utf-8") as f:
                    f.write(text)

                print(f"[ICEQ] Arquivo salvo: {self._editor_current_file}")
                self._program_open_path(self._editor_current_file)
                self._program_refresh_ui_after_load(self._editor_current_file, text=text)
                return


            # =========================================================
            # CENÁRIO B — Novo arquivo (Salvar Como)
            # =========================================================
            from PyQt5.QtWidgets import QFileDialog

            file_path, _ = QFileDialog.getSaveFileName(
                self,
                "Salvar G-code",
                "/home/cnc/linuxcnc/nc_files",
                "G-code (*.ngc *.nc *.gcode)"
            )

            if not file_path:
                print("[ICEQ] Salvar cancelado pelo usuário.")
                return

            # Garante extensão
            if not file_path.lower().endswith((".ngc", ".nc", ".gcode")):
                file_path += ".ngc"

            with open(file_path, "w", encoding="utf-8") as f:
                f.write(text)

            self._editor_current_file = file_path


            print(f"[ICEQ] Novo arquivo salvo: {file_path}")
            self._program_open_path(file_path)
            self._program_refresh_ui_after_load(file_path, text=text)


        except Exception as e:
            print(f"[ICEQ][ERRO] Falha ao salvar arquivo: {e}")


    def _update_program_name_labels(self, file_path):
        import os

        if not file_path:
            name = "Nenhum Gcode carregado"
        else:
            name = os.path.basename(file_path)

        text = f"<b>{name}</b>"

        if hasattr(self, "lbl_program_name"):
            self.lbl_program_name.setText(text)

        if hasattr(self, "lbl_program_name2"):
            self.lbl_program_name2.setText(text)

    def _program_refresh_ui_after_load(self, file_path, text=None):
        """
        Após carregar/salvar um programa, sincroniza:
        - Aba PROGRAMA (se existir um viewer de texto)
        - Preview 2D (w_preview_2d / preview2d)
        """
        try:
            if not file_path:
                return

            # 1) Atualiza viewer da aba PROGRAMA (se existir)
            #    (Nome do widget pode variar, então usamos hasattr)
            if text is not None:
                if hasattr(self, "txt_gcode_view"):
                    try:
                        self.txt_gcode_view.setPlainText(str(text))
                    except Exception:
                        pass
                elif hasattr(self, "txt_program_view"):
                    try:
                        self.txt_program_view.setPlainText(str(text))
                    except Exception:
                        pass
                elif hasattr(self, "pte_program"):
                    try:
                        self.pte_program.setPlainText(str(text))
                    except Exception:
                        pass

            # 2) Força reload do Preview 2D imediatamente (sem depender do stat.file)
            if hasattr(self, "preview2d") and self.preview2d:
                try:
                    self.preview2d.ensure_program_loaded(str(file_path))
                    self._preview2d_last_stat_file = str(file_path)
                except Exception:
                    pass

        except Exception as e:
            print(f"[ICEQ] Erro em _program_refresh_ui_after_load: {e}")


    def _load_program_after_save(self, file_path, text=None):
        """
        Após salvar no Editor, carrega o arquivo como se fosse o botão Abrir:
        - program_open no LinuxCNC
        - atualiza a aba PROGRAMA (txt_gcode_view)
        - força reload do preview 2D
        """
        try:
            if not file_path:
                return

            # Fallback interno para labels/estado (caso stat.file demore)
            self._program_loaded_path = str(file_path)

            # 1) Carrega no LinuxCNC
            try:
                with self._cmd_lock:
                    self.cmd.program_open(str(file_path))
                    try:
                        self.cmd.wait_complete()
                    except Exception:
                        pass
            except Exception as e:
                print(f"[ICEQ] Erro ao carregar programa no LinuxCNC (program_open): {e}")

            # 2) Atualiza conteúdo da aba PROGRAMA imediatamente
            if text is not None and hasattr(self, "txt_gcode_view"):
                try:
                    self.txt_gcode_view.setPlainText(str(text))
                except Exception as e:
                    print(f"[ICEQ] Erro ao atualizar txt_gcode_view: {e}")

            # 3) Força reload do preview 2D imediatamente (sem esperar stat.file)
            try:
                if hasattr(self, "preview2d") and self.preview2d:
                    self.preview2d.ensure_program_loaded(str(file_path))
                    self._preview2d_last_stat_file = str(file_path)
            except Exception as e:
                print(f"[ICEQ] Erro ao atualizar preview2d: {e}")

        except Exception as e:
            print(f"[ICEQ] Erro em _load_program_after_save: {e}")


    # ---------------------------------------------------------
    # MDI (comandos manuais + histórico)
    # ---------------------------------------------------------
    def _mdi_history_file_path(self):
        import os
        ini_path = os.environ.get("INI_FILE_NAME") or os.environ.get("EMC2_INI_FILE_NAME") or ""
        base_dir = ""
        if ini_path:
            try:
                base_dir = os.path.dirname(str(ini_path))
            except Exception:
                base_dir = ""

        # fallback (caso não exista INI no env por algum motivo)
        if not base_dir:
            base_dir = os.path.join(os.path.expanduser("~"), ".iceq")

        try:
            os.makedirs(base_dir, exist_ok=True)
        except Exception:
            pass

        return os.path.join(base_dir, "iceq_mdi_history.txt")

    def _mdi_history_load(self):
        import os

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
            import os

            path = self._mdi_history_file_path()
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
            except Exception:
                pass

            with open(path, "a", encoding="utf-8") as f:
                f.write(str(text).replace("\r", "") + "\n")

            # trim (só quando crescer demais, para não pesar)
            max_lines = int(getattr(self, "_mdi_hist_max_lines", 400) or 400)
            if max_lines > 0:
                try:
                    with open(path, "r", encoding="utf-8", errors="ignore") as f:
                        all_lines = f.readlines()

                    if len(all_lines) > (max_lines * 2):
                        all_lines = all_lines[-max_lines:]
                        with open(path, "w", encoding="utf-8") as f:
                            f.writelines(all_lines)
                except Exception:
                    pass

        except Exception:
            pass

    def _append_mdi_history(self, text):
        if not hasattr(self, "txt_mdi_history"):
            return

        try:
            # QPlainTextEdit
            self.txt_mdi_history.appendPlainText(text)
            try:
                sb = self.txt_mdi_history.verticalScrollBar()
                sb.setValue(sb.maximum())
            except Exception:
                pass
        except Exception:
            # fallback mínimo (caso widget seja QTextEdit ou algo diferente)
            try:
                self.txt_mdi_history.append(text)
            except Exception:
                pass

        # Persistência em disco (para manter histórico após reiniciar)
        try:
            self._mdi_history_append_disk(text)
        except Exception:
            pass


    def eventFilter(self, obj, event):
        """
        Pickup do histórico MDI:
        ao clicar em uma linha do txt_mdi_history, copia a linha para txt_mdi_entry
        (padrão AXIS: agiliza repetição de comandos).
        """
        try:
            if hasattr(self, "txt_mdi_history"):
                w = self.txt_mdi_history

                if obj is w or (hasattr(w, "viewport") and obj is w.viewport()):
                    et = event.type()

                    # Clique esquerdo: ao soltar o botão, faz o pickup da linha clicada
                    if et == QtCore.QEvent.MouseButtonRelease:
                        try:
                            if event.button() == QtCore.Qt.LeftButton:
                                self._mdi_history_pick_to_entry(event, from_viewport=(obj is not w))
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

            # posição do clique: se veio do viewport, já está no sistema correto para cursorForPosition
            pos = event.pos() if event is not None else None
            if pos is None:
                return

            cur = w.cursorForPosition(pos)
            cur.select(QtGui.QTextCursor.LineUnderCursor)
            line = str(cur.selectedText()).strip()

            if not line:
                return

            # Opcional: não “puxa” linhas de erro automáticas
            if line.upper().startswith("ERR:"):
                return

            self.txt_mdi_entry.setText(line)
            self.txt_mdi_entry.setFocus()
            try:
                self.txt_mdi_entry.selectAll()
            except Exception:
                pass

        except Exception as e:
            print(f"[ICEQ] MDI history pickup: erro: {e}")




    def _run_mdi_command(self, cmd_text):
        """
        Executa um comando no canal MDI do LinuxCNC.
        Serializado por lock para evitar deadlock com threads.
        """
        try:
            with self._cmd_lock:
                # garante modo MDI antes de mandar o comando
                self.cmd.mode(linuxcnc.MODE_MDI)
                self.cmd.wait_complete()

                self.cmd.mdi(cmd_text)
                self.cmd.wait_complete()

            return True, ""
        except Exception as e:
            return False, str(e)


    def on_mdi_send(self):
        """
        Lê txt_mdi_entry, registra no histórico e executa MDI SEM travar a UI.
        Também detecta troca de ferramenta via MDI (Tn/M6) e atualiza a label.
        """
        if not hasattr(self, "txt_mdi_entry"):
            return

        try:
            cmd_text = self.txt_mdi_entry.text().strip()
        except Exception:
            return

        if not cmd_text:
            return

        # 1) registra no histórico
        self._append_mdi_history(f"{cmd_text}")

        # 2) evita reentrância
        if bool(getattr(self, "_mdi_busy", False)):
            self._append_mdi_history("ERR: MDI ocupado (aguarde finalizar)")
            return

        self._mdi_busy = True

        # 3) limpa e foca para o próximo comando (IMEDIATO)
        try:
            self.txt_mdi_entry.clear()
            self.txt_mdi_entry.setFocus()
        except Exception:
            pass

        # 4) inicia FSM não-bloqueante
        self._start_mdi_fsm(cmd_text)

    def _start_mdi_fsm(self, cmd_text: str):
        """
        FSM não-bloqueante para executar MDI sem cmd.wait_complete().
        Critério de fim: stat.interp_state voltar para INTERP_IDLE (com timeout).
        """
        try:
            self._mdi_last_cmd = str(cmd_text)

            # Marca se este MDI é uma troca (contém M6)
            try:
                s = str(cmd_text).strip().upper()
                self._mdi_is_toolchange = bool(re.search(r"\bM\s*6\b", s))
            except Exception:
                self._mdi_is_toolchange = False

            # Guarda modo anterior (ajuda a não “prender” em MDI após toolchange)
            try:
                self.stat.poll()
                self._mdi_prev_mode = int(getattr(self.stat, "task_mode", linuxcnc.MODE_MANUAL))
            except Exception:
                self._mdi_prev_mode = int(linuxcnc.MODE_MANUAL)

            self._mdi_fsm_state = "MDI_MODE"
            self._mdi_fsm_sent = False
            self._mdi_sent_ts = 0.0

            # Timeout geral do MDI (toolchange costuma precisar mais)
            self._mdi_deadline = time.time() + (120.0 if bool(getattr(self, "_mdi_is_toolchange", False)) else 60.0)

            if self._mdi_fsm_timer is None:
                self._mdi_fsm_timer = QtCore.QTimer(self)
                self._mdi_fsm_timer.timeout.connect(self._mdi_fsm_tick)

            self._mdi_fsm_timer.start(50)

        except Exception as e:
            # Falha ao iniciar FSM
            self._append_mdi_history(f"ERR: falha iniciando MDI FSM: {e}")
            self._mdi_busy = False



    def _mdi_fsm_tick(self):
        """
        Tick da FSM do MDI.
        Não usa wait_complete. Apenas envia e espera interp voltar para IDLE.
        """
        try:
            # Timeout duro
            if time.time() > float(getattr(self, "_mdi_deadline", 0.0) or 0.0):
                raise RuntimeError("timeout aguardando fim do MDI (FSM)")

            try:
                self.stat.poll()
            except Exception:
                pass

            st = str(getattr(self, "_mdi_fsm_state", ""))

            # 1) Garantir modo MDI (sem wait_complete)
            if st == "MDI_MODE":
                try:
                    self.cmd.mode(linuxcnc.MODE_MDI)
                except Exception:
                    pass

                # Avança quando o stat refletir MDI (ou tenta mesmo assim depois de alguns ticks)
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

            # 2) Enviar comando (uma vez)
            if st == "SEND":
                if not bool(getattr(self, "_mdi_fsm_sent", False)):
                    try:
                        with self._cmd_lock:
                            self.cmd.mdi(str(getattr(self, "_mdi_last_cmd", "")))
                    except Exception:
                        # mesmo se falhar, deixa cair no handler de erro do try/except externo
                        raise

                    self._mdi_fsm_sent = True
                    self._mdi_sent_ts = time.time()
                    self._mdi_fsm_state = "WAIT_IDLE"
                return

            # 3) Esperar fim do MDI
            if st == "WAIT_IDLE":
                try:
                    interp = int(getattr(self.stat, "interp_state", linuxcnc.INTERP_IDLE))
                except Exception:
                    interp = linuxcnc.INTERP_IDLE

                is_tc = bool(getattr(self, "_mdi_is_toolchange", False))

                # Caso NÃO seja toolchange: mantém critério clássico (interp voltar IDLE)
                if not is_tc:
                    if interp != linuxcnc.INTERP_IDLE:
                        return
                    self._mdi_fsm_state = "DONE"
                    return

                # Caso SEJA toolchange (M6):
                # - aceita conclusão quando o hardware da troca já desligou (P6/P7 OFF),
                #   mesmo se o interpreter ainda não voltou para IDLE.
                try:
                    hw_active = bool(self._toolchange_hw_active())
                except Exception:
                    hw_active = False

                # Dá um pequeno “assentamento” após o envio do MDI
                sent_ts = float(getattr(self, "_mdi_sent_ts", 0.0) or 0.0)
                if hw_active:
                    return

                if sent_ts > 0.0 and (time.time() - sent_ts) < 0.20:
                    return

                # Concluiu toolchange pelo hardware
                self._mdi_fsm_state = "DONE"
                return


            # 4) DONE -> finaliza e para timer
            if st == "DONE":
                try:
                    if self._mdi_fsm_timer is not None:
                        self._mdi_fsm_timer.stop()
                except Exception:
                    pass

                self._mdi_finish(str(getattr(self, "_mdi_last_cmd", "")), True, "")
                return

        except Exception as e:
            # Erro -> abort e finaliza
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
        """
        Finaliza no thread da UI: loga erro e faz o tool-detect (Tn/M6),
        preservando exatamente a lógica que você já tinha no on_mdi_send.
        """
        try:
            if not ok:
                self._append_mdi_history(f"ERR: {err}")

            # 2.1) Se foi troca de ferramenta via MDI, atualiza a UI
            try:
                s = str(cmd_text).strip().upper()

                # Caso A: "Tn M6" (mesma linha)
                m = re.search(r"\bT\s*([0-9]{1,2})\b.*\bM\s*6\b", s)
                if m and ok:
                    tn = int(m.group(1))
                    self._tool_active_virtual = tn
                    self._tool_active_last = None
                    self._update_active_tool_label(tn)
                    self._mdi_pending_tool = 0
                else:
                    # Caso B: "Tn" sozinho
                    m2 = re.fullmatch(r"\s*T\s*([0-9]{1,2})\s*", s)
                    if m2 and ok:
                        self._mdi_pending_tool = int(m2.group(1))

                    # Caso C: "M6" sozinho
                    m3 = re.fullmatch(r"\s*M\s*6\s*", s)
                    if m3 and ok and int(getattr(self, "_mdi_pending_tool", 0) or 0) > 0:
                        tn = int(self._mdi_pending_tool)
                        self._tool_active_virtual = tn
                        self._tool_active_last = None
                        self._update_active_tool_label(tn)
                        self._mdi_pending_tool = 0

            except Exception as e:
                print(f"[ICEQ] MDI tool-detect erro: {e}")

        finally:
            # Se foi toolchange via MDI, restaura o modo anterior (evita ficar preso em MDI)
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



    # ---------------------------------------------------------
    # Homing helpers (Lathe: X=0, Z=2)
    # ---------------------------------------------------------
    def _wait_for_homed(self, axes, timeout_s=90.0):
        """
        Aguarda até todos os eixos da lista 'axes' ficarem homed.
        Retorna True se concluiu, False se estourou timeout.
        """
        t0 = time.time()
        while True:
            try:
                self.stat.poll()
            except Exception:
                pass

            try:
                if all(bool(self.stat.homed[a]) for a in axes):
                    return True
            except Exception:
                # Se por algum motivo stat.homed não estiver disponível ainda
                pass

            if (time.time() - t0) >= float(timeout_s):
                return False

            try:
                QtWidgets.QApplication.processEvents()
            except Exception:
                pass

            time.sleep(0.05)

    # --------------------------------------------------
    # ----------- REFERENCIA DE TODOS OS HOME ----------
    # --------------------------------------------------
    def _axis_joint_index(self, axis_letter):
        """Map axis letter (X/Y/Z/...) to joint index based on stat.axis_mask."""
        try:
            self.stat.poll()
        except Exception:
            pass
        axes_bits = [('X', 1), ('Y', 2), ('Z', 4), ('A', 8), ('B', 16), ('C', 32), ('U', 64), ('V', 128), ('W', 256)]
        enabled = [name for name, bit in axes_bits if (getattr(self.stat, 'axis_mask', 0) & bit)]
        if axis_letter not in enabled:
            raise ValueError(f"Axis {axis_letter} not enabled (axis_mask={getattr(self.stat, 'axis_mask', 0)})")
        return enabled.index(axis_letter)

    def _request_home(self, axes, timeout_s=60.0):
        """Request homing for the given axes list, waiting until homed (non-blocking via thread)."""
        if getattr(self, '_homing_thread', None) and self._homing_thread.is_alive():
            self._log('[ICEQ] homing: já em andamento, ignorando novo comando')
            return

        def _worker():
            try:
                self.stat.poll()
                if self.stat.estop:
                    self._log('[ICEQ] homing: em E-STOP, não é possível referenciar')
                    return
                if not self.stat.enabled:
                    self._log('[ICEQ] homing: máquina desabilitada (POWER OFF), não é possível referenciar')
                    return

                # Ensure MANUAL mode for homing
                try:
                    self.cmd.mode(linuxcnc.MODE_MANUAL)
                    self.cmd.wait_complete()
                except Exception:
                    pass

                if axes == ['ALL']:
                    self._log('[ICEQ] HOME ALL (todos os eixos)')
                    self.cmd.home(-1)
                    joints = None
                else:
                    joints = [self._axis_joint_index(a) for a in axes]
                    for a, j in zip(axes, joints):
                        self._log(f"[ICEQ] HOME {a} (joint {j})")
                        self.cmd.home(j)

                t0 = time.time()
                while (time.time() - t0) < float(timeout_s):
                    self.stat.poll()
                    if self.stat.estop or (not self.stat.enabled):
                        self._log('[ICEQ] homing: abortado (E-STOP ou máquina OFF)')
                        return

                    if axes == ['ALL']:
                        axes_bits = [('X', 1), ('Y', 2), ('Z', 4), ('A', 8), ('B', 16), ('C', 32), ('U', 64), ('V', 128), ('W', 256)]
                        enabled = [name for name, bit in axes_bits if (getattr(self.stat, 'axis_mask', 0) & bit)]
                        if enabled and all(self.stat.homed[self._axis_joint_index(a)] for a in enabled):
                            self._log('[ICEQ] HOME ALL concluído')
                            return
                    else:
                        if all(self.stat.homed[j] for j in joints):
                            self._log(f"[ICEQ] HOME {' '.join(axes)} concluído")
                            return
                    time.sleep(0.05)

                self._log('[ICEQ] homing: TIMEOUT aguardando referência')
            except Exception as e:
                self._log(f"[ICEQ] homing: erro: {e}")

        self._homing_thread = threading.Thread(target=_worker, daemon=True)
        self._homing_thread.start()

    AXIS_X = 0
    AXIS_Y = 1
    AXIS_Z = 2
    # ------------------ REF (HOME) ------------------

    def _dbg(self, msg: str):
        # Mantém log consistente e evita crash por ausência de método
        self._dbg_last_status = msg
        print(f"[ICEQ] {msg}")

    # =========================================================
    # CLOUD — Logger de transições (sem flood)
    # =========================================================
    def _cloud_now_ts(self) -> float:
        try:
            return float(time.time())
        except Exception:
            return 0.0

    def _cloud_rate_ok(self, key: str) -> bool:
        """
        Evita flood: limita envios repetidos do mesmo 'key' dentro de _cloud_min_interval_s.
        """
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
        """
        Envia um log de transição com payload padronizado.
        """
        try:
            if not bool(getattr(self, "_cloud_enabled", False)):
                return
            if not (self.cloud and self.cloud.is_configured()):
                return
        except Exception:
            return

        # rate-limit por evento (segurança)
        if not self._cloud_rate_ok(f"tr:{event}"):
            return

        try:
            payload = {
                "event": str(event),
                "state": state or {},
                "prev": prev or {},
                "source": "ihm",
            }

            # IMPORTANTE:
            # Aqui usamos send_log() (ou equivalente).
            # Se teu IceqCloudClient tiver outro nome (ex.: send_machine_log),
            # me diga o método existente que eu ajusto sem mexer em mais nada.
            self.cloud.send_log(
                log_type="transition",
                payload=payload,
                severity="info",
            )
        except Exception as e:
            try:
                print(f"[ICEQ][CLOUD] falha enviando transição: {e}")
            except Exception:
                pass

    def _cloud_collect_state(self) -> dict:
        """
        Coleta um snapshot pequeno, estável e útil para detectar mudanças.
        Não depende de UI.
        """
        st = {}
        try:
            self.stat.poll()
        except Exception:
            pass

        # E-STOP / Máquina
        try:
            st["estop"] = bool(getattr(self.stat, "estop", False))
        except Exception:
            st["estop"] = False

        try:
            st["enabled"] = bool(getattr(self.stat, "enabled", False))
        except Exception:
            st["enabled"] = False

        # Execução (AUTO/IDLE/paused)
        try:
            st["task_mode"] = int(getattr(self.stat, "task_mode", -1))
        except Exception:
            st["task_mode"] = -1

        try:
            st["interp_state"] = int(getattr(self.stat, "interp_state", -1))
        except Exception:
            st["interp_state"] = -1

        try:
            st["paused"] = bool(getattr(self.stat, "paused", False))
        except Exception:
            st["paused"] = False

        # Spindle (feedback)
        try:
            sp_on, sp_dir = self._get_spindle_fb()
            st["spindle_on"] = bool(sp_on)
            st["spindle_dir"] = int(sp_dir)
        except Exception:
            st["spindle_on"] = False
            st["spindle_dir"] = 0

        # Coolant (usa teu estado interno, que já está consistente no painel)
        try:
            st["coolant"] = bool(getattr(self, "_coolant_on", False))
        except Exception:
            st["coolant"] = False

        # Ferramenta
        try:
            st["tool"] = int(getattr(self.stat, "tool_in_spindle", 0))
        except Exception:
            st["tool"] = int(getattr(self, "_tool_active_virtual", 0) or 0)

        # Troca ativa (flag estável)
        try:
            st["toolchange_active"] = bool(getattr(self, "_toolchange_busy", False))
        except Exception:
            st["toolchange_active"] = False

        return st

    def _cloud_ping_tick(self):
        """
        Timer: mantém online/heartbeat.
        """
        try:
            if not bool(getattr(self, "_cloud_enabled", False)):
                return
            if not (self.cloud and self.cloud.is_configured()):
                return
            self.cloud.send_ping()
        except Exception as e:
            try:
                print(f"[ICEQ][CLOUD] ping falhou: {e}")
            except Exception:
                pass

    def _cloud_transition_tick(self):
        """
        Timer: detecta mudanças e envia log somente quando muda.
        """
        try:
            if not bool(getattr(self, "_cloud_enabled", False)):
                return

            cur = self._cloud_collect_state()
            prev = getattr(self, "_cloud_last_state", None)

            # Primeira coleta: só armazena (não gera log)
            if not isinstance(prev, dict):
                self._cloud_last_state = cur
                return

            # Detecta mudanças relevantes
            changed = []
            for k in ("estop", "enabled", "task_mode", "interp_state", "paused",
                      "spindle_on", "spindle_dir", "coolant", "tool", "toolchange_active"):
                if prev.get(k) != cur.get(k):
                    changed.append(k)

            if not changed:
                return

            # Define um "evento" compacto (prioridade)
            if "estop" in changed:
                event = "estop_changed"
            elif "enabled" in changed:
                event = "machine_enabled_changed"
            elif ("task_mode" in changed) or ("interp_state" in changed) or ("paused" in changed):
                event = "program_state_changed"
            elif ("spindle_on" in changed) or ("spindle_dir" in changed):
                event = "spindle_state_changed"
            elif "coolant" in changed:
                event = "coolant_changed"
            elif "toolchange_active" in changed:
                event = "toolchange_activity_changed"
            elif "tool" in changed:
                event = "tool_changed"
            else:
                event = "state_changed"

            # Anexa lista do que mudou (ajuda no dashboard)
            cur2 = dict(cur)
            cur2["_changed_keys"] = changed

            self._cloud_send_transition_log(event=event, state=cur2, prev=prev)

            # Atualiza snapshot
            self._cloud_last_state = cur

        except Exception as e:
            try:
                print(f"[ICEQ][CLOUD] transition tick falhou: {e}")
            except Exception:
                pass


    def _is_any_homing(self) -> bool:
        try:
            self.stat.poll()
            # stat.joint[j].homing existe no LinuxCNC 2.8
            for j in range(len(self.stat.joint)):
                if getattr(self.stat.joint[j], "homing", False):
                    return True
        except Exception:
            pass
        return False

    def _wait_for_homed(self, joints, timeout_s: float) -> bool:
        import time
        t0 = time.time()
        while (time.time() - t0) < timeout_s:
            self.stat.poll()

            ok = True
            for j in joints:
                # stat.homed é lista por joint
                if j >= len(self.stat.homed) or not self.stat.homed[j]:
                    ok = False
                    break

            if ok:
                return True

            time.sleep(0.05)
        return False

    def _start_homing_thread(self, joints, timeout_s: float = 90.0):
        import threading

        # Evita reentrância (X termina e você clica Z e “não acontece nada”)
        if getattr(self, "_homing_busy", False):
            self._dbg("Homing já em andamento; ignorando novo comando")
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

            # Modo MANUAL é obrigatório para home
            self.cmd.mode(linuxcnc.MODE_MANUAL)
            self.cmd.wait_complete()

            # Dispara HOME de cada eixo e espera concluir antes do próximo
            for j in joints:
                self._dbg(f"HOME joint {j}: disparando")
                self.cmd.home(j)
                self.cmd.wait_complete()

                ok = self._wait_for_homed([j], timeout_s=timeout_s)
                if not ok:
                    self._dbg(f"HOME joint {j}: TIMEOUT (sensor/home/limits?)")
                    return

                self._dbg(f"HOME joint {j}: ok")

            self._dbg("HOME concluído")

        except Exception as e:
            self._dbg(f"Erro no HOMING: {e}")

        finally:
            self._homing_busy = False

    def ref_all(self):
        # X e Z (no teu setup: 0 e 2)
        try:
            self._dbg("HOME ALL (XZ)")
            self._start_homing_thread([0, 1], timeout_s=90.0)
        except Exception as e:
            self._dbg(f"Erro ao referenciar (HOME ALL): {e}")

    def ref_x(self):
        try:
            self._dbg("HOME X")
            self._start_homing_thread([0], timeout_s=60.0)
        except Exception as e:
            self._dbg(f"Erro ao referenciar X: {e}")

    def ref_z(self):
        try:
            self._dbg("HOME Z")
            self._start_homing_thread([1], timeout_s=60.0)
        except Exception as e:
            self._dbg(f"Erro ao referenciar Z: {e}")


    # ---------------------------------------------------------
    # ZERO PECA (G54) — zera offsets da peca no sistema G54
    # ---------------------------------------------------------
    def zero_g54(self):
        try:
            print("[ICEQ] ZERO PECA (G54)")
            self.cmd.mode(linuxcnc.MODE_MDI)
            self.cmd.wait_complete()

            # Zera offsets X e Z do G54 em relacao à posicao atual
            self.cmd.mdi("G10 L20 P1 X0 Z0")
            self.cmd.wait_complete()

            print("[ICEQ] G54 zerado")
        except Exception as e:
            print(f"[ICEQ] Erro em zero_g54: {e}")

    # ---------------------------------------------------------
    # ----------  COORDENADAS DO CABECALHO E RPM DO RODAPE ----
    # ---------------------------------------------------------
    def _set_label_if_exists(self, attr_name, text):
        """Seta texto em QLabel/QLineEdit se existir no .ui."""
        try:
            w = getattr(self, attr_name, None)
            if w is None:
                return
            if hasattr(w, "setText"):
                w.setText(text)
        except Exception as e:
            print(f"[ICEQ] _set_label_if_exists erro ({attr_name}): {e}")

    def _get_spindle_rpm_safe(self):
        """
        Retorna o RPM REAL do spindle.

        Prioridade:
        1) Encoder (feedback HAL), se existir
        2) RPM comandado (S) × spindle override (fallback para SIM)
        """

        # ------------------------------------------------------------
        # 1) Tenta RPM REAL via encoder (HAL)
        # ------------------------------------------------------------
        try:
            # Exemplo de nomes comuns (ajustaremos quando você definir o final):
            #   spindle.0.speed-in
            #   motion.spindle-speed-in
            #   encoder.0.velocity (convertido)
            import hal

            # Tente os pinos mais prováveis (sem quebrar se não existirem)
            for pin in (
                "spindle.0.speed-in",
                "motion.spindle-speed-in",
            ):
                try:
                    if hasattr(hal, "get_value"):
                        v = hal.get_value(pin)
                        if v is not None:
                            rpm = abs(float(v))
                            if rpm > 0.1:
                                return rpm
                except Exception:
                    pass
        except Exception:
            pass

        # ------------------------------------------------------------
        # 2) Fallback: RPM comandado × override (SIM / sem encoder)
        # ------------------------------------------------------------
        try:
            # RPM base do comando (S do G-code)
            rpm_base = None
            try:
                rpm_base = float(self.stat.spindle[0]['speed'])
            except Exception:
                pass

            if rpm_base is None:
                return 0.0

            # Override atual
            try:
                ovr = int(getattr(self, "_spindle_ovr_pct", 100))
            except Exception:
                ovr = 100

            ovr = max(0, min(120, ovr))
            rpm_eff = abs(rpm_base) * (float(ovr) / 100.0)
            return rpm_eff

        except Exception:
            return 0.0

    # ------------------------------------------------------------
    # SPINDLE / COOLANT - lógica consolidada (sem RPM negativo no display)
    # ------------------------------------------------------------
    def _spindle_start_if_zero(self):
        """Se clicar CW/CCW com RPM zerado, inicia em 100 RPM."""
        try:
            if int(self._spindle_rpm_setpoint) <= 0:
                self._spindle_rpm_setpoint = 100
        except Exception:
            self._spindle_rpm_setpoint = 100

    def _spindle_apply(self):
        """
        Aplica setpoint interno + direção interna no LinuxCNC.
        Envia RPM sempre positivo; sentido é pelo dir (1/-1).
        """
        try:
            rpm = int(abs(self._spindle_rpm_setpoint))

            if self._spindle_dir == 0 or rpm <= 0:
                self.cmd.spindle(0)
                return

            self.cmd.spindle(int(self._spindle_dir), float(rpm))

        except Exception as e:
            print(f"[ICEQ] spindle_apply erro: {e}")


    # ============================================================
    # SPINDLE — CONTROLE MANUAL (estado interno confiável)
    # ============================================================
    def spindle_cw(self):
        try:
            if self._spindle_rpm_setpoint <= 0:
                self._spindle_rpm_setpoint = 100

            self.cmd.spindle(
                linuxcnc.SPINDLE_FORWARD,
                self._spindle_rpm_setpoint
            )

            self._spindle_dir = 1
            self._spindle_running = True

        except Exception as e:
            print(f"[ICEQ] spindle_cw erro: {e}")

        self._dbg("Spindle CW (M3)")

    def spindle_ccw(self):
        try:
            if self._spindle_rpm_setpoint <= 0:
                self._spindle_rpm_setpoint = 100

            self.cmd.spindle(
                linuxcnc.SPINDLE_REVERSE,
                self._spindle_rpm_setpoint
            )

            self._spindle_dir = -1
            self._spindle_running = True

        except Exception as e:
            print(f"[ICEQ] spindle_ccw erro: {e}")

        self._dbg("Spindle CCW (M4)")

    def spindle_stop(self):
        try:
            self.cmd.spindle(linuxcnc.SPINDLE_OFF)

            self._spindle_dir = 0
            self._spindle_running = False
            self._spindle_rpm_setpoint = 0

        except Exception as e:
            print(f"[ICEQ] spindle_stop erro: {e}")

        self._dbg("Spindle STOP (M5)")

    # ============================================================
    # COOLANT — CONTROLE MANUAL
    # ============================================================
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

    def spindle_rpm_plus(self):
        """
        Aumenta RPM em steps.
        Se spindle estiver girando (dir != 0), aplica na hora.
        """
        try:
            self._spindle_rpm_setpoint = int(self._spindle_rpm_setpoint) + int(self._spindle_step)
            if self._spindle_rpm_setpoint < 0:
                self._spindle_rpm_setpoint = 0

            if self._spindle_dir != 0:
                self._spindle_apply()

        except Exception as e:
            print(f"[ICEQ] spindle_rpm_plus erro: {e}")

        self._dbg("Spindle RPM +")

    def spindle_rpm_minus(self):
        """
        Diminui RPM em steps.
        Regra: ao chegar em 0 -> desliga o spindle automaticamente.
        """
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

        self._dbg("Spindle RPM -")

    def _update_spindle_rpm_label(self):
        """
        Atualiza o label de RPM da tela usando o setpoint interno.
        Sempre mostra positivo (corrige o problema do negativo).
        """
        try:
            rpm_disp = int(abs(self._spindle_rpm_setpoint))
            # Ajuste aqui somente se seu objectName for diferente:
            self._set_label_if_exists("lbl_spindle_rpm", f"{rpm_disp:d} RPM")
        except Exception as e:
            print(f"[ICEQ] update_spindle_rpm_label erro: {e}")


    def _init_spindle_rpm_bar(self):
        """
        Barra vertical de RPM do spindle como FUNDO das labels:
        - lbl_rpm_tittle (título)
        - lbl_spindle_rpm (valor)
        Sem depender de alteração no .ui.
        """
        from PyQt5.QtWidgets import QProgressBar
        from PyQt5.QtCore import Qt

        if not hasattr(self, "lbl_spindle_rpm"):
            return

        title_lbl = getattr(self, "lbl_rpm_tittle", None)
        value_lbl = getattr(self, "lbl_spindle_rpm", None)
        if value_lbl is None:
            return

        parent = value_lbl.parentWidget()
        if parent is None:
            return

        # Se já existe, não recria
        if getattr(self, "_spindle_rpm_bar", None) is not None:
            return

        bar = QProgressBar(parent)
        bar.setObjectName("bar_spindle_rpm")
        bar.setOrientation(Qt.Vertical)
        bar.setTextVisible(False)
        bar.setMinimum(0)
        bar.setMaximum(1000)
        bar.setValue(0)

        # Não deixa a barra “roubar” clique/toque (importante em UI touch)
        try:
            bar.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        except Exception:
            pass

        # Estilo com seletor específico (vence tema global e evita “tudo branco”)
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

        # Guarda e faz o 1º posicionamento + z-order
        self._spindle_rpm_bar = bar
        self._spindle_rpm_bar_relayout()

    def _spindle_rpm_bar_relayout(self):
        """
        Recalcula área da barra para ocupar o fundo das duas labels
        e garante Z-order correto (barra atrás, labels na frente).
        """
        try:
            bar = getattr(self, "_spindle_rpm_bar", None)
            if bar is None:
                return

            title_lbl = getattr(self, "lbl_rpm_tittle", None)
            value_lbl = getattr(self, "lbl_spindle_rpm", None)
            if value_lbl is None:
                return

            rect = value_lbl.geometry()
            if title_lbl is not None:
                rect = rect.united(title_lbl.geometry())

            # Força as dimensões que você pediu (140 x 120) como mínimo
            w = max(int(rect.width()), 140)
            h = max(int(rect.height()), 120)
            rect.setWidth(w)
            rect.setHeight(h)

            # ajuste fino de alinhamento vertical (negativo sobe / positivo desce)
            Y_OFFSET = -60
            rect.translate(0, Y_OFFSET)

            m = 2
            bar.setGeometry(rect.adjusted(m, m, -m, -m))


            # Barra sempre no fundo
            try:
                bar.lower()
            except Exception:
                pass

            # Labels sempre na frente
            try:
                if title_lbl is not None:
                    title_lbl.raise_()
                value_lbl.raise_()
            except Exception:
                pass

            # Labels com fundo transparente para “deixar ver” a barra atrás
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
        """
        Atualiza valor da barra com base no RPM efetivo.
        Usa self._spindle_rpm_max_val (lido do INI) como máximo, quando disponível.
        """
        try:
            bar = getattr(self, "_spindle_rpm_bar", None)
            if bar is None:
                return

            # Garante geometria e z-order corretos mesmo após layout/tema
            self._spindle_rpm_bar_relayout()


            # máximo vindo do INI (já lido no update_status_panel)
            max_rpm = int(getattr(self, "_spindle_rpm_max_val", 0) or 0)
            if max_rpm <= 0:
                max_rpm = int(bar.maximum() or 1000)

            if bar.maximum() != max_rpm:
                bar.setMaximum(max_rpm)

            v = float(rpm_value or 0.0)
            if v < 0:
                v = -v
            v = max(0.0, min(float(max_rpm), v))

            iv = int(round(v))
            if bar.value() != iv:
                bar.setValue(iv)

        except Exception:
            pass


    def _get_coolant_on_safe(self):
        """Retorna True/False para mist/flood."""
        try:
            mist = bool(getattr(self.stat, "mist", False))
            flood = bool(getattr(self.stat, "flood", False))
            return (mist or flood)
        except Exception:
            return False


    # -----------------------------------------------------------------
    # ------ funcao auxiliar que grifa a linha executada no gcode -----
    # -----------------------------------------------------------------
    def _on_right_tab_changed(self, idx):
        """
        Quando o usuário abre a aba PROGRAMA, reaplica o grifo imediatamente.
        Isso resolve o problema clássico: 'só grifa quando eu clico'.
        """
        try:
            # TROQUE "idx_programa" se a ordem das abas for diferente.
            # Se você não souber o índice, eu te passo como pegar por nome.
            idx_programa = 0

            if idx == idx_programa:
                if self._pending_gcode_line is not None:
                    self._highlight_gcode_line(self.txt_gcode_view, self._pending_gcode_line)
        except Exception as e:
            print(f"[ICEQ] tab changed erro: {e}")

    # ------------------------------------------------------------------------------------
    # --------------------  funcao auxiliar do contador e barra de progresso do gcode ----
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
        """
        Conta linhas "úteis" do G-code para estimar progresso.
        Remove vazias e comentários simples (; e linhas iniciando com '(' ).
        """
        try:
            total = 0
            for ln in text.splitlines():
                s = ln.strip()
                if not s:
                    continue
                if s.startswith(";"):
                    continue
                if s.startswith("("):
                    continue
                total += 1
            return max(1, total)
        except Exception:
            return 0

    # -------------------------------------------------------------
    # ------- controle de status dos pinos do spindle e coolant ---
    # -------------------------------------------------------------
    def _hal_bit(self, pin_name: str):
        """
        Lê um pino HAL bit e retorna:
        - True/False se conseguiu ler
        - None se o pino não existir / erro
        """
        # 1) Tentativa rápida via API HAL Python
        try:
            import hal
            v = hal.get_value(pin_name)
            return bool(v)
        except Exception as e:
            # Debug (1x por sessão/pino) para entender por que o hal.get_value falhou
            try:
                if not hasattr(self, "_halbit_err_once"):
                    self._halbit_err_once = {}
                if not self._halbit_err_once.get(pin_name, False):
                    self._halbit_err_once[pin_name] = True
                    print(f"[ICEQ][HAL] get_value falhou em '{pin_name}': {e}")
            except Exception:
                pass

        # 2) Fallback robusto via halcmd (lento, mas confiável para o toolchange)
        try:
            import subprocess
            out = subprocess.check_output(["halcmd", "getp", pin_name], stderr=subprocess.STDOUT, text=True).strip()
            # halcmd pode retornar: TRUE/FALSE, 0/1, ou números
            u = out.upper()
            if u in ("TRUE", "1"):
                return True
            if u in ("FALSE", "0"):
                return False
            # números não-inteiros: considera != 0 como True
            try:
                return float(out) != 0.0
            except Exception:
                return None
        except Exception:
            return None



    def _hal_bit_multi(self, pin_names):
        """
        Tenta ler uma lista de pinos HAL (primeiro que existir).
        Retorna True/False se conseguir ler; retorna None se nenhum existir.
        """
        for p in pin_names:
            v = self._hal_bit(p)
            if v is not None:
                return v
        return None

    def _init_hal_jog_component(self):
        """
        Cria um HAL component 'iceqjog' para dirigir sinais de JOG CONTÍNUO via HALUI.
        Isso evita usar halcmd (que falha quando pinos halui.* estão netados).
        """
        try:
            import hal
        except Exception:
            print("[ICEQ][JOG] HAL python module não disponível (import hal falhou)")
            return

        try:
            c = hal.component("iceqjog")

            # bits (saídas do Python -> entram no HALUI)
            c.newpin("jog-x-pos", hal.HAL_BIT, hal.HAL_OUT)
            c.newpin("jog-x-neg", hal.HAL_BIT, hal.HAL_OUT)
            c.newpin("jog-z-pos", hal.HAL_BIT, hal.HAL_OUT)
            c.newpin("jog-z-neg", hal.HAL_BIT, hal.HAL_OUT)

            # velocidade (mm/min) para halui.axis.jog-speed (float IN)
            c.newpin("jog-speed", hal.HAL_FLOAT, hal.HAL_OUT)

            # inicializa tudo em zero
            c["jog-x-pos"] = 0
            c["jog-x-neg"] = 0
            c["jog-z-pos"] = 0
            c["jog-z-neg"] = 0
            c["jog-speed"] = 0.0

            c.ready()

            self._hal_jog_comp = c
            self._hal_jog_ready = True

            print("[ICEQ][JOG] HAL component 'iceqjog' criado e pronto")

        except Exception as e:
            self._hal_jog_comp = None
            self._hal_jog_ready = False
            print(f"[ICEQ][JOG] Falha criando HAL component iceqjog: {e}")

    def _hal_jog_set(self, pin_name, value):
        """
        Seta pino do component iceqjog de forma segura.
        """
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

    def _jog_continuous_start(self, axis, direction):
        """
        axis: "X" ou "Z"
        direction: +1 ou -1
        """
        # segurança: sempre começa zerando tudo
        self._hal_jog_all_off()

        # seta velocidade (mm/min) vinda da sua lógica atual (não altera)
        try:
            vcur = float(getattr(self, "_jog_speed_current_mm_min", 0.0) or 0.0)
            if vcur <= 0.0:
                # fallback: se você usa pct + vmax internamente
                vmax = float(getattr(self, "_jog_speed_max_mm_min", 0.0) or 0.0)
                pct = float(getattr(self, "_jog_speed_pct", 100.0) or 100.0)
                if vmax > 0.0:
                    vcur = vmax * (pct / 100.0)
        except Exception:
            vcur = 0.0

        if vcur > 0.0:
            self._hal_jog_set("jog-speed", float(vcur))

        # seta direção
        if str(axis).upper() == "Z":
            if int(direction) > 0:
                self._hal_jog_set("jog-z-pos", 1)
            else:
                self._hal_jog_set("jog-z-neg", 1)
        else:
            if int(direction) > 0:
                self._hal_jog_set("jog-x-pos", 1)
            else:
                self._hal_jog_set("jog-x-neg", 1)

    def _jog_continuous_stop(self):
        self._hal_jog_all_off()


    def _hal_jog_all_off(self):
        """
        Desliga todos os bits de JOG contínuo (segurança).
        """
        self._hal_jog_set("jog-x-pos", 0)
        self._hal_jog_set("jog-x-neg", 0)
        self._hal_jog_set("jog-z-pos", 0)
        self._hal_jog_set("jog-z-neg", 0)


    def _get_spindle_fb(self):
        """
        Feedback do spindle via linuxcnc.stat (fallback robusto para SIM e máquina real).
        Retorna: (spindle_on_fb: bool, spindle_dir_fb: int)
          - dir: +1 (CW), -1 (CCW), 0 (parado/indefinido)
        """
        spindle_on_fb = False
        spindle_dir_fb = 0
        try:
            # LinuxCNC normalmente expõe spindle como array: self.stat.spindle[0]
            sp = self.stat.spindle[0]

            # enabled costuma refletir spindle "ligado"
            spindle_on_fb = bool(getattr(sp, "enabled", False))

            # direction normalmente: 1 CW, -1 CCW, 0 parado
            spindle_dir_fb = int(getattr(sp, "direction", 0))

            # fallback extra: se enabled não vier confiável, usa velocidade
            if not spindle_on_fb:
                try:
                    s = float(getattr(sp, "speed", 0.0))
                    spindle_on_fb = abs(s) > 0.0
                except Exception:
                    pass

        except Exception:
            pass

        return spindle_on_fb, spindle_dir_fb


    def _set_state_label(self, widget_name: str, value: bool):
        """Atualiza QLabel de estado (TRUE/FALSE) se existir."""
        if hasattr(self, widget_name):
            getattr(self, widget_name).setText("TRUE" if value else "FALSE") 

    # ============================================================
    # ESTATUS ABA MONITOR - DA ABA FERRAMENTAS (TORRE)
    # ============================================================

    def _hal_out_from_label(self, label_widget_name: str):
        """
        Interpreta o texto de um QLabel PIN (ex.: 'OUT6' ou 'motion.digital-out-06')
        e tenta ler o bit correspondente via HAL.

        Retorna: True/False ou None (se não conseguiu ler).
        """
        try:
            if not hasattr(self, label_widget_name):
                return None

            txt = str(getattr(self, label_widget_name).text()).strip()
            if not txt:
                return None

            u = txt.upper()

            # Permite informar o nome do pino HAL diretamente no .ui (ex.: 'hm2_7i96s.0.gpio.024.out')
            if "." in txt:
                return self._hal_bit(txt)

            # Formato OUTn -> motion.digital-out-n (com zero pad também)
            if u.startswith("OUT"):
                num = int(u.replace("OUT", "").strip())
                cand = [
                    f"motion.digital-out-{num:02d}",
                    f"motion.digital-out-{num}",
                ]
                return self._hal_bit_multi(cand)

        except Exception:
            return None

        return None



    # ============================================================
    # SLIDER CALLBACKS - Overrides industriais
    # ============================================================

    def _on_vel_machine_changed(self, value):
        """
        Velocidade da máquina:
        - Feed Override
        - Spindle Override
        Atua durante AUTO e MDI
        """
        try:
            value = int(value)
            value = max(0, min(120, value))

            self._vel_machine_pct = value

            # Sincroniza slider <-> spinbox
            if hasattr(self, "sld_vel_machine_oper") and self.sld_vel_machine_oper.value() != value:
                self.sld_vel_machine_oper.blockSignals(True)
                self.sld_vel_machine_oper.setValue(value)
                self.sld_vel_machine_oper.blockSignals(False)

            if hasattr(self, "spn_vel_machine_oper") and self.spn_vel_machine_oper.value() != value:
                self.spn_vel_machine_oper.blockSignals(True)
                self.spn_vel_machine_oper.setValue(value)
                self.spn_vel_machine_oper.blockSignals(False)

            # Feed Override (%)
            self.cmd.feedrate(value)

            # Spindle Override (%)
            self.cmd.spindleoverride(value)

        except Exception as e:
            print(f"[ICEQ] erro vel_machine slider: {e}")


    def _on_vel_spindle_changed(self, value):
        """
        Velocidade do spindle:
        - Spindle Override
        - 0% => STOP real do spindle
        """
        try:
            value = int(value)
            value = max(0, min(120, value))

            self._vel_spindle_pct = value

            # Sincroniza slider <-> spinbox
            if hasattr(self, "sld_vel_spindle_oper") and self.sld_vel_spindle_oper.value() != value:
                self.sld_vel_spindle_oper.blockSignals(True)
                self.sld_vel_spindle_oper.setValue(value)
                self.sld_vel_spindle_oper.blockSignals(False)

            if hasattr(self, "spn_vel_spindle_oper") and self.spn_vel_spindle_oper.value() != value:
                self.spn_vel_spindle_oper.blockSignals(True)
                self.spn_vel_spindle_oper.setValue(value)
                self.spn_vel_spindle_oper.blockSignals(False)

            # 0% => STOP spindle
            if value == 0:
                self.cmd.spindle(linuxcnc.SPINDLE_OFF)
                return

            # Spindle Override (%)
            self.cmd.spindleoverride(value)

        except Exception as e:
            print(f"[ICEQ] erro vel_spindle slider: {e}")

    # ============================================================
    # JOG (BÁSICO por CLIQUE) - X e Z
    # - Move por incremento usando MDI: G91 G0 X... / Z... e volta G90
    # - Roda em thread para NÃO travar a UI
    # ============================================================

    def _jog_get_step_mm(self) -> float:
        """
        Tenta ler o passo do combo (cmb_jog_step).
        Aceita textos tipo: '0.1', '0,1', '0.10 mm', '1mm', etc.
        Se falhar, usa default.
        """
        try:
            if hasattr(self, "cmb_jog_step"):
                txt = str(self.cmb_jog_step.currentText()).strip()
                if txt:
                    txt = txt.replace(",", ".")
                    m = re.search(r"([-+]?\d*\.?\d+)", txt)
                    if m:
                        v = float(m.group(1))
                        if v > 0.0:
                            return float(v)
        except Exception:
            pass

        try:
            return float(getattr(self, "_jog_step_mm_default", 0.1))
        except Exception:
            return 0.1

    def _jog_machine_ready(self) -> (bool, str):
        """
        Intertravamento mínimo industrial para JOG:
        - sem E-STOP
        - máquina ON
        - não estar rodando programa em AUTO (interp != IDLE)
        - X/Z homed (opcional mas recomendado para evitar surpresas)
        """
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

        # Se estiver em AUTO rodando, bloqueia
        try:
            mode = int(getattr(self.stat, "task_mode", -1))
            interp = int(getattr(self.stat, "interp_state", -1))
            if mode == int(linuxcnc.MODE_AUTO) and interp != int(linuxcnc.INTERP_IDLE):
                return False, "programa em execução (AUTO)"
        except Exception:
            pass

        # Homed X/Z (no seu torno: joints 0 e 1)
        try:
            if hasattr(self.stat, "homed"):
                hx = bool(self.stat.homed[0])
                hz = bool(self.stat.homed[1])
                if not (hx and hz):
                    return False, "não referenciado (X/Z)"
        except Exception:
            pass

        # Evita reentrância
        if bool(getattr(self, "_jog_busy", False)):
            return False, "JOG ocupado"

        return True, ""

    def _is_jog_incremental_mode(self) -> bool:
        """
        Retorna True somente quando o modo estiver em 'Incremental'.
        Fontes possíveis:
        - btn_jog_mode (novo)
        - cb_jog_mode (legado)
        """
        try:
            if hasattr(self, "btn_jog_mode"):
                t = str(self.btn_jog_mode.text()).strip().lower()
            elif hasattr(self, "cb_jog_mode"):
                t = str(self.cb_jog_mode.currentText()).strip().lower()
            else:
                return True  # se não existir, não bloqueia (compatibilidade)

            return ("increment" in t)
        except Exception:
            return False


    def _on_jog_mode_changed(self, _text: str):
        """
        Atualiza o estado dos botões de JOG conforme o modo.
        Em Contínuo: desabilita (não responde).
        Em Incremental: habilita.
        """
        # Agora: os botões ficam habilitados nos dois modos.
        # - Incremental: usa clique (_jog_click)
        # - Contínuo: usa pressed/released (HALUI)
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
        """
        Alterna o modo do JOG no botão btn_jog_mode:
        Contínuo <-> Incremental
        """
        try:
            opts = list(getattr(self, "_jog_mode_options", ["Contínuo", "Incremental"]))
            if not opts:
                opts = ["Contínuo", "Incremental"]

            idx = int(getattr(self, "_jog_mode_idx", 0))
            idx = (idx + 1) % len(opts)
            self._jog_mode_idx = idx

            txt = str(opts[idx])

            try:
                self.btn_jog_mode.setText(txt)
            except Exception:
                pass

            # Reusa sua lógica atual (habilita/desabilita botões)
            self._on_jog_mode_changed(txt)
        except Exception as e:
            print(f"[ICEQ][JOG] erro alternando modo: {e}")

    def _on_btn_jog_step_clicked(self):
        """
        Alterna o passo do JOG no botão btn_jog_step.
        Mantém o seu parser atual via _on_jog_step_changed(text).
        """
        try:
            opts = list(getattr(self, "_jog_step_options", [10.0, 1.0, 0.5, 0.1, 0.01, 0.001]))
            if not opts:
                opts = [10.0, 1.0, 0.5, 0.1, 0.01, 0.001]

            idx = int(getattr(self, "_jog_step_idx", 0))
            idx = (idx + 1) % len(opts)
            self._jog_step_idx = idx

            v = float(opts[idx])
            v_txt = f"{v:g}"

            try:
                self.btn_jog_step.setText(f"{v_txt} mm")
            except Exception:
                pass

            # Reusa sua lógica atual (atualiza self._jog_step_mm)
            self._on_jog_step_changed(v_txt)
        except Exception as e:
            print(f"[ICEQ][JOG] erro alternando passo: {e}")

    def _is_jog_continuous_mode(self) -> bool:
        """True quando modo estiver em 'Contínuo'."""
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

    def _jog_halui_pin_candidates(self, axis: str, direction: int):
        """
        Retorna lista de pinos HALUI possíveis para jog contínuo.
        Tenta por letra (axis.x/axis.z) e por índice (axis.0/axis.1).
        No torno típico: Z=0, X=1.
        """
        ax = str(axis).upper().strip()
        plus = (int(direction) >= 0)

        if ax == "Z":
            idx = 0
            letter = "z"
        else:
            idx = 1
            letter = "x"

        suf = "plus" if plus else "minus"

        return (
            f"halui.axis.{letter}.{suf}",
            f"halui.axis.{idx}.{suf}",
            f"halui.axis.{idx}.{suf}-fast",
            f"halui.axis.{letter}.{suf}-fast",
        )

    def _hal_set_bit_any(self, pins, value: bool) -> bool:
        """Tenta setar o primeiro pin que aceitar escrita. Retorna True se conseguiu."""
        try:
            v = "TRUE" if bool(value) else "FALSE"
            for p in pins:
                try:
                    if hasattr(hal, "pin_has_writer") and hal.pin_has_writer(p):
                        hal.set_p(p, v)
                        return True
                    # fallback: tenta mesmo assim
                    hal.set_p(p, v)
                    return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    def _jog_get_speed_mm_min(self):
        """
        Retorna velocidade atual do JOG em mm/min, garantindo > 0.
        Prioridade:
          1) _jog_speed_current_mm_min (se >0)
          2) calcula por pct * vmax
          3) parse do lbl_jog_value (se existir)
        """
        try:
            vcur = float(getattr(self, "_jog_speed_current_mm_min", 0.0) or 0.0)
            if vcur > 0.0:
                return vcur

            vmax = float(getattr(self, "_jog_speed_max_mm_min", 0.0) or 0.0)
            pct = float(getattr(self, "_jog_speed_pct", 100) or 100)
            if vmax > 0.0:
                vcur = vmax * (pct / 100.0)
                if vcur > 0.0:
                    return vcur

            # fallback: tenta ler do texto da label (ex: "1000 mm/min")
            try:
                if hasattr(self, "lbl_jog_value"):
                    t = str(self.lbl_jog_value.text() or "").strip()
                    # pega somente números e ponto/vírgula
                    t = t.replace(",", ".")
                    num = ""
                    for ch in t:
                        if (ch.isdigit() or ch == "."):
                            num += ch
                        elif num:
                            break
                    v = float(num) if num else 0.0
                    if v > 0.0:
                        return v
            except Exception:
                pass
        except Exception:
            pass

        return 0.0


    def _jog_cont_signal_names(self, axis):
        """
        Retorna os SIGNALs HAL usados no contínuo (seleção + jog).
        Usa:
            axis-select-x / axis-select-z
            jog-selected-pos / jog-selected-neg
            jog-speed
        """
        a = str(axis).strip().upper()

        # cache para evitar erro "no attribute" e evitar recomputar
        if not hasattr(self, "_jog_cont_signal_map"):
            self._jog_cont_signal_map = {
                "X": ("axis-select-x", "axis-select-z"),
                "Z": ("axis-select-z", "axis-select-x"),
            }

        if a not in self._jog_cont_signal_map:
            raise ValueError(f"Axis inválido para JOG contínuo: {a}")

        return self._jog_cont_signal_map[a]



    def _jog_get_speed_mm_min(self):
        """
        Retorna velocidade atual do JOG em mm/min.
        Prioridade:
          1) self._jog_speed_current_mm_min
          2) vmax * pct (self._jog_speed_max_mm_min e self._jog_speed_pct)
          3) parse do lbl_jog_value (se existir)
        """
        try:
            v = float(getattr(self, "_jog_speed_current_mm_min", 0.0) or 0.0)
            if v > 0.0:
                return v

            vmax = float(getattr(self, "_jog_speed_max_mm_min", 0.0) or 0.0)
            pct = float(getattr(self, "_jog_speed_pct", 0.0) or 0.0)
            if vmax > 0.0 and pct > 0.0:
                return vmax * (pct / 100.0)

            # Fallback: tenta ler a label (ex: "1000 mm/min")
            if hasattr(self, "lbl_jog_value"):
                txt = str(self.lbl_jog_value.text() or "").strip().lower()
                # pega apenas o número
                num = ""
                for ch in txt:
                    if ch.isdigit() or ch in ".,":
                        num += ch
                num = num.replace(",", ".").strip()
                if num:
                    return float(num)
        except Exception:
            pass
        return 0.0




    def _jog_continuous_press(self, axis, direction):
        """
        CONTÍNUO:
        - Garante MODE_MANUAL (HALUI só joga em MANUAL)
        - Seleciona eixo via axis-select-x / axis-select-z
        - Aplica velocidade em jog-speed
        - Liga jog-selected-pos ou jog-selected-neg enquanto botão estiver pressionado
        """
        try:
            a = str(axis).strip().upper()
            d = 1 if int(direction) >= 1 else -1

            # 1) garante MANUAL (não bloqueia UI)
            try:
                with self._cmd_lock:
                    self.cmd.mode(linuxcnc.MODE_MANUAL)
            except Exception:
                # se não conseguir, ainda tenta setar HAL (mas normalmente não vai mover)
                pass

            # 2) pega velocidade atual (mm/min) com fallback seguro
            vcur = float(getattr(self, "_jog_speed_current_mm_min", 0.0) or 0.0)
            if vcur <= 0.0:
                # fallback: usa máximo configurado do JOG
                vcur = float(getattr(self, "_jog_speed_max_mm_min", 0.0) or 0.0)

            # se ainda ficou 0, não adianta tentar contínuo
            if vcur <= 0.0:
                print(f"[ICEQ][JOG] contínuo bloqueado: velocidade JOG = 0 (verifique slider/label)")
                return

            # 3) seleciona eixo (axis-select-*)
            sig_on, sig_off = self._jog_cont_signal_names(a)
            self._hal_sets_signal(sig_off, 0)
            self._hal_sets_signal(sig_on, 1)

            # 4) seta velocidade do HALUI
            self._hal_sets_signal("jog-speed", vcur)

            # 5) liga o sentido usando o “selected”
            if d >= 1:
                self._hal_sets_signal("jog-selected-neg", 0)
                ok = self._hal_sets_signal("jog-selected-pos", 1)
            else:
                self._hal_sets_signal("jog-selected-pos", 0)
                ok = self._hal_sets_signal("jog-selected-neg", 1)

            print(f"[ICEQ][JOG] HALUI SETS: {a} dir={d} (Vel: {vcur:.1f})")

            if not ok:
                print(f"[ICEQ][JOG] contínuo: falha ao setar jog-selected (confira nets no HAL)")

        except Exception as e:
            print(f"[ICEQ][JOG] contínuo erro ao iniciar ({axis}): {e}")





    def _jog_continuous_release(self, axis):
        """
        CONTÍNUO: soltar botão = desliga jog-selected-pos e jog-selected-neg
        (não precisa mexer em axis-select aqui)
        """
        try:
            self._hal_sets_signal("jog-selected-pos", 0)
            self._hal_sets_signal("jog-selected-neg", 0)
            print(f"[ICEQ][JOG] HALUI RELEASE: jog-selected-pos / jog-selected-neg = 0")
        except Exception as e:
            print(f"[ICEQ][JOG] contínuo erro ao parar ({axis}): {e}")







    def _jog_continuous_worker(self, a, d):
        """
        Worker do contínuo para evitar travar a UI:
        - força MODE_MANUAL
        - seleciona eixo via axis-select-*
        - aplica jog-speed
        - liga jog-*-pos/neg enquanto estiver pressionado
        """
        try:
            # 1) Garante modo MANUAL (HALUI só joga no MANUAL)
            try:
                with self._cmd_lock:
                    self.cmd.mode(linuxcnc.MODE_MANUAL)
                    self.cmd.wait_complete()
            except Exception:
                # se falhar por qualquer motivo, ainda tenta setar os nets (não trava)
                pass

            # Se soltou muito rápido, não liga nada
            if not getattr(self, "_jog_cont_pressed", {}).get(a, False):
                return

            # 2) Seleção do eixo (lathe: alterna X/Z)
            sel_sig = "axis-select-x" if a == "X" else "axis-select-z"
            other_sig = "axis-select-z" if a == "X" else "axis-select-x"

            self._hal_sets_signal(other_sig, 0)
            self._hal_sets_signal(sel_sig, 1)

            # 3) Velocidade do JOG (mm/min) no sinal jog-speed
            vcur = float(getattr(self, "_jog_speed_current_mm_min", 0.0) or 0.0)
            if vcur > 0.0:
                self._hal_sets_signal("jog-speed", vcur)

            # 4) Liga direção
            if a == "X":
                sig_pos, sig_neg = "jog-x-pos", "jog-x-neg"
            else:
                sig_pos, sig_neg = "jog-z-pos", "jog-z-neg"

            if d > 0:
                self._hal_sets_signal(sig_neg, 0)
                ok, err = self._hal_sets_signal(sig_pos, 1)
                if not ok:
                    print(f"[ICEQ][JOG] contínuo: falha set {sig_pos}=1 ({err})")
            else:
                self._hal_sets_signal(sig_pos, 0)
                ok, err = self._hal_sets_signal(sig_neg, 1)
                if not ok:
                    print(f"[ICEQ][JOG] contínuo: falha set {sig_neg}=1 ({err})")

            print(f"[ICEQ][JOG] HALUI SETS: {'jog-'+a.lower()+'-pos' if d > 0 else 'jog-'+a.lower()+'-neg'} = 1 (Vel: {vcur:.1f})")
        except Exception as e:
            print(f"[ICEQ][JOG] erro worker contínuo ({a}): {e}")



    def _jog_continuous_tick(self):
        """
        Chamado a cada 100ms enquanto o botão está apertado.
        Calcula a distância para andar em 100ms com a velocidade atual e envia.
        """
        try:
            # 1. Ler Velocidade (mm/min)
            vel_mm_min = 600.0
            try:
                if hasattr(self, "lbl_jog_value"):
                    txt = self.lbl_jog_value.text().lower().replace("mm/min", "").strip()
                    vel_mm_min = float(txt)
            except:
                pass
                
            # Garante mínimo
            if vel_mm_min < 10: vel_mm_min = 100.0
            
            # 2. Calcula incremento para 100ms (0.1s)
            # Distancia = Velocidade * Tempo
            # Dist (mm) = (Vel (mm/min) / 60) * 0.1s
            dist_step = (vel_mm_min / 60.0) * 0.1
            
            # Aplica direção
            dist_final = dist_step * self._jog_direction
            
            # 3. Envia comando JOG INCREMENTAL (Tipo 0)
            # cmd.jog(INCREMENTAL, Axis, Vel, Dist)
            # Axis: X=0, Z=2 (Tentativa padrão Cartesiana)
            # Se não mover Z, trocamos para 1 (Joint) aqui depois.
            axis_idx = 0 if self._jog_axis_letter == "X" else 2 
            
            # Envia velocidade um pouco maior (120%) para garantir que o movimento termine antes do próximo tick
            vel_cmd = (vel_mm_min / 60.0) * 1.2
            
            # Envia! (Usando JOG_INCREMENTAL = 0)
            # Nota: JOG_INCREMENTAL aceita float na maioria das versões, mas se der erro voltamos pra int.
            self.cmd.jog(linuxcnc.JOG_INCREMENTAL, axis_idx, vel_cmd, dist_step)
            
            # Debug leve (comenta se poluir muito)
            # print(f"Tick: {self._jog_axis_letter} dist={dist_step:.4f}")

        except Exception as e:
            print(f"[ICEQ][JOG] erro Loop Tick: {e}")
            # Para o timer em caso de erro para não travar tudo
            self._jog_timer.stop()







    def _jog_is_mode_continuous(self):
        """
        Modo contínuo: pelo texto do botão btn_jog_mode.
        (Você já alterna Contínuo/Incremental no botão.)
        """
        try:
            if hasattr(self, "btn_jog_mode"):
                txt = (self.btn_jog_mode.text() or "").strip().lower()
                return ("cont" in txt)  # "Contínuo"
        except Exception:
            pass
        return False

    def _jog_is_mode_incremental(self):
        try:
            if hasattr(self, "btn_jog_mode"):
                txt = (self.btn_jog_mode.text() or "").strip().lower()
                return ("incr" in txt)  # "Incremental"
        except Exception:
            pass
        return False

    def _jog_press(self, axis, direction):
        """
        Wrapper do JOG contínuo (para manter compatibilidade com as conexões atuais).
        """
        self._jog_continuous_press(axis, direction)



    def _jog_release(self, axis):
        """
        Wrapper do JOG contínuo (para manter compatibilidade com as conexões atuais).
        """
        self._jog_continuous_release(axis)



    def _jog_click(self, axis_letter: str, direction: int):
        """
        Jog incremental seguro por JOINT (não por AXIS):
        - calcula destino virtual
        - valida contra soft-limits reais do joint
        - nunca derruba Machine ON
        """
        try:
            axis = str(axis_letter).upper().strip()
            if axis not in ("X", "Z"):
                return

            # Gate do modo: só permite JOG quando estiver em INCREMENTAL
            # OBS: no modo CONTÍNUO o movimento é por pressed/released; o clicked pode disparar ao soltar.
            if not self._is_jog_incremental_mode():
                if self._jog_is_mode_continuous():
                    return
                print("[ICEQ][JOG] bloqueado: modo CONTÍNUO (selecione INCREMENTAL)")
                return


            # Se estiver em CONTÍNUO, ignora o click (clicked acontece no release do botão)
            # e deixa o controle do movimento contínuo só no pressed/released.
            if self._jog_is_mode_continuous():
                return


            ok, reason = self._jog_machine_ready()
            if not ok:
                print(f"[ICEQ][JOG] bloqueado: {reason}")
                return

            # Passo configurado
            step_mm = float(getattr(self, "_jog_step_mm", 0.1))
            step_mm *= (1.0 if int(direction) >= 0 else -1.0)

            # Descobre qual joint corresponde ao eixo
            # Torno típico: joint 0 = Z, joint 1 = X
            try:
                # Mapeamento explícito (mais seguro que stat.axis)
                joint_map = {
                    "Z": 0,
                    "X": 1,
                }
                joint_idx = joint_map[axis]
            except Exception:
                print("[ICEQ][JOG] falha no mapeamento eixo->joint")
                return

            # Lê posição atual do JOINT
            try:
                self.stat.poll()
                joints = list(getattr(self.stat, "joint_position", []))
            except Exception:
                print("[ICEQ][JOG] falha ao ler joint_position")
                return

            if joint_idx >= len(joints):
                print("[ICEQ][JOG] joint fora do range")
                return

            cur_pos = float(joints[joint_idx])

            # Conversão mm -> unidade da máquina
            try:
                lu = float(getattr(self.stat, "linear_units", 1.0))
            except Exception:
                lu = 1.0

            def mm_to_machine(v_mm: float) -> float:
                if lu < 0.999:
                    return v_mm * lu
                elif lu > 1.001:
                    return v_mm / lu
                return v_mm

            delta_mu = mm_to_machine(step_mm)
            target_mu = cur_pos + delta_mu

            # Valida contra limites reais do JOINT
            try:
                joints_cfg = list(getattr(self.stat, "joint", []))
                if joint_idx < len(joints_cfg):
                    lo = float(getattr(joints_cfg[joint_idx], "min_position_limit", -1e9))
                    hi = float(getattr(joints_cfg[joint_idx], "max_position_limit",  1e9))
                    if not (lo <= target_mu <= hi):
                        print(
                            f"[ICEQ][JOG] bloqueado por limite do joint {joint_idx}: "
                            f"{target_mu:.3f} fora [{lo:.3f}, {hi:.3f}]"
                        )
                        return
            except Exception:
                pass

            # Pode executar o jog
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
        """
        Worker: envia UM MDI contendo (G91 move ; G90) para não quebrar o MDI depois.
        NÃO troca modo de volta aqui. A GUI libera quando o interp voltar IDLE.
        """
        try:
            # converte mm -> unidade da máquina (inch/mm/etc) usando linear_units
            try:
                self.stat.poll()
                lu = float(getattr(self.stat, "linear_units", 1.0))
            except Exception:
                lu = 1.0

            def mm_to_machine(v_mm: float) -> float:
                try:
                    v_mm = float(v_mm)
                    # inverso do teu to_mm()
                    if lu < 0.999:      # ex.: 0.03937 (inch/mm) -> machine = mm * lu (inch)
                        return v_mm * lu
                    elif lu > 1.001:    # ex.: 25.4 (mm/inch) -> machine = mm / lu
                        return v_mm / lu
                    return v_mm
                except Exception:
                    return float(v_mm)

            dist_mu = mm_to_machine(dist_mm)

            # IMPORTANTE:
            # G90 e G91 são do mesmo grupo modal -> não podem estar no mesmo bloco.
            # ------------------------------------------------------------
            # Velocidade do JOG (mm/min) -> aplica via G1 F...
            # - G0 ignora F (rapid), então para o JOG respeitar a velocidade,
            #   usamos G1 com feed calculado.
            # - Depois restauramos G0 (modal) para não "vazar" G1 para outros MDIs.
            # ------------------------------------------------------------
            try:
                vmax_mm_min = float(getattr(self, "_jog_speed_max_mm_min", 1000.0))
                pct = int(getattr(self, "_jog_speed_pct", 100))
                pct = max(0, min(120, pct))
                vcur_mm_min = vmax_mm_min * (float(pct) / 100.0)
            except Exception:
                vcur_mm_min = 1000.0

            # Converte mm/min -> unidade da máquina / min (inch/min se for o caso)
            try:
                feed_mu_min = mm_to_machine(vcur_mm_min)
            except Exception:
                feed_mu_min = vcur_mm_min

            # evita feed zero/absurdo
            try:
                if float(feed_mu_min) < 1.0:
                    feed_mu_min = 1.0
            except Exception:
                feed_mu_min = 1.0

            # IMPORTANTE:
            # G90 e G91 são do mesmo grupo modal -> não podem estar no mesmo bloco.
            # Movimento incremental com G1 + F (primeira linha)
            cmd_move_inc = f"G91 G1 {axis}{dist_mu:.5f} F{float(feed_mu_min):.1f}"
            # Volta absoluto (segunda linha)
            cmd_abs = "G90"
            # Restaura modo de movimento para rápido (terceira linha, sem mover)
            cmd_restore_rapid = "G0"

            print(f"[ICEQ][JOG] {axis} click -> {cmd_move_inc}  |  {cmd_abs}  |  {cmd_restore_rapid}")

            with self._cmd_lock:
                try:
                    self.cmd.mode(linuxcnc.MODE_MDI)
                except Exception:
                    pass

                # 1) move em incremental com FEED calculado
                self.cmd.mdi(cmd_move_inc)

                # 2) volta para absoluto
                self.cmd.mdi(cmd_abs)

                # 3) restaura rápido (modal) para não deixar G1 ativo
                self.cmd.mdi(cmd_restore_rapid)


        except Exception as e:
            try:
                self._jog_last_error = str(e)
            except Exception:
                pass
            print(f"[ICEQ][JOG] exceção worker: {e}")
        finally:
            # A liberação de _jog_busy fica a cargo do _jog_finish_tick (IDLE/timeout)
            pass




    def _jog_finish_tick(self):
        """
        Tick da GUI para finalizar JOG:
        - libera _jog_busy quando interp voltar IDLE
        - restaura o modo anterior (MANUAL/AUTO) SOMENTE depois de IDLE
        - ou libera por timeout (para nunca travar)
        """
        # Se por algum motivo não está busy, só garante timer parado
        if not bool(getattr(self, "_jog_busy", False)):
            try:
                if getattr(self, "_jog_finish_timer", None) is not None:
                    self._jog_finish_timer.stop()
            except Exception:
                pass
            return

        # Poll rápido
        try:
            self.stat.poll()
        except Exception:
            pass

        # Checa IDLE
        try:
            interp_idle = int(getattr(linuxcnc, "INTERP_IDLE", 1))
            interp_state = int(getattr(self.stat, "interp_state", interp_idle))
            is_idle = (interp_state == interp_idle)
        except Exception:
            is_idle = True

        # Timeout duro
        try:
            if time.time() > float(getattr(self, "_jog_finish_deadline", 0.0) or 0.0):
                print(f"[ICEQ][JOG] timeout esperando IDLE ({getattr(self, '_jog_finish_axis', '')})")
                is_idle = True
        except Exception:
            pass

        if not is_idle:
            return

        # Segurança: garante modo absoluto após qualquer JOG (não deixa “vazar” G91)
        try:
            with self._cmd_lock:
                self.cmd.mode(linuxcnc.MODE_MDI)
                self.cmd.mdi("G90")
        except Exception:
            pass

        # Agora sim: restaurar modo anterior (pra não “prender” em MDI)
        try:
            prev = int(getattr(self, "_jog_prev_mode", linuxcnc.MODE_MANUAL))
            back = prev if prev in (linuxcnc.MODE_MANUAL, linuxcnc.MODE_AUTO) else linuxcnc.MODE_MANUAL
            try:
                self.cmd.mode(back)
            except Exception:
                pass
        except Exception:
            pass

        # libera busy e para timer
        self._jog_busy = False
        try:
            if getattr(self, "_jog_finish_timer", None) is not None:
                self._jog_finish_timer.stop()
        except Exception:
            pass




    # ------------------------------------------------------------
    # JOG - callback do ComboBox de passo
    # ------------------------------------------------------------
    def _on_jog_step_changed(self, text: str):
        """
        Atualiza o passo de JOG a partir do ComboBox.
        Aceita:
            1
            0,5
            0,1
            0,01
            0,001
            10 mm
        """
        try:
            if not text:
                return

            txt = str(text).strip().lower()

            # normaliza vírgula para ponto
            txt = txt.replace(",", ".")

            # extrai número (aceita '10 mm', '0.1', etc)
            m = re.search(r"([-+]?\d*\.?\d+)", txt)
            if not m:
                return

            value = float(m.group(1))

            if value <= 0.0:
                return

            self._jog_step_mm = value
            print(f"[ICEQ][JOG] passo atualizado: {self._jog_step_mm} mm")

        except Exception as e:
            print(f"[ICEQ][JOG] erro ao ler passo: {e}")

    # ============================================================
    # JOG - VELOCIDADE (% -> mm/min) + UI Sync
    # ============================================================

    def _get_jog_max_mm_min_safe(self) -> float:
        """
        Retorna a velocidade máxima de JOG em mm/min, baseada no INI.
        Estratégia:
          - Lê o INI em INI_FILE_NAME (LinuxCNC normalmente exporta isso)
          - Usa o maior MAX_VELOCITY entre AXIS_X e AXIS_Z (ou TRAJ)
          - Converte para mm/min considerando linear_units
        Fallback seguro: 1000 mm/min
        """
        try:
            import os
            import configparser

            ini_path = (
                os.environ.get("INI_FILE_NAME")
                or os.environ.get("EMC2_INI_FILE_NAME")
                or os.environ.get("LINUXCNC_INI")
                or getattr(self, "_ini_path", "")
            )
            if not ini_path:
                return 0.0


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

            # MAX_VELOCITY geralmente está em "unidades por segundo"
            vx = _getf("AXIS_X", "MAX_VELOCITY", None)
            vz = _getf("AXIS_Z", "MAX_VELOCITY", None)

            # fallback por TRAJ (algumas configs usam isso)
            vt = _getf("TRAJ", "MAX_LINEAR_VELOCITY", None)
            if vt is None:
                vt = _getf("TRAJ", "MAX_VELOCITY", None)

            # escolhe o maior disponível
            candidates = [v for v in (vx, vz, vt) if isinstance(v, (int, float)) and v > 0.0]
            if not candidates:
                return 1000.0

            v_units_per_s = max(candidates)

            # Converte para mm/min usando linear_units (igual teu cabeçalho)
            try:
                self.stat.poll()
                lu = float(getattr(self.stat, "linear_units", 1.0))
            except Exception:
                lu = 1.0

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

            v_mm_per_s = to_mm(v_units_per_s)
            v_mm_min = v_mm_per_s * 60.0

            # mínimo “humano” pra não ficar zero
            if v_mm_min <= 1.0:
                return 1000.0

            return float(v_mm_min)

        except Exception:
            return 1000.0

    def _get_traj_max_mm_min_safe(self) -> float:
        """
        Retorna a velocidade máxima "de máquina" (G0/rápido) em mm/min baseada no INI.

        Estratégia:
          - Lê INI_FILE_NAME / EMC2_INI_FILE_NAME / LINUXCNC_INI
          - Pega MAX_LINEAR_VELOCITY (ou MAX_VELOCITY) em [TRAJ] (unidades/segundo)
          - Limita pelo maior MAX_VELOCITY encontrado em AXIS_X/AXIS_Z ou JOINT_0/JOINT_1 (também em unidades/segundo)
          - Converte para mm/min respeitando LINEAR_UNITS (mm ou inch)
        Fallback seguro: 0.0 (indefinido)
        """
        try:
            import os
            import re
            import configparser

            ini_path = (
                os.environ.get("INI_FILE_NAME")
                or os.environ.get("EMC2_INI_FILE_NAME")
                or os.environ.get("LINUXCNC_INI")
                or ""
            )
            if not ini_path:
                return 0.0

            # strict=False é obrigatório porque teu INI tem chaves repetidas (ex.: MAX_LINEAR_VELOCITY duplicado)
            cfg = configparser.RawConfigParser(
                inline_comment_prefixes=(";", "#"),
                strict=False
            )
            loaded = cfg.read(ini_path)
            if not loaded:
                return 0.0

            def _parse_ini_number(raw):
                if raw is None:
                    return None
                s = str(raw).strip()
                s = s.split(";")[0].split("#")[0].strip()
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
                    v = cfg.get(section, key, fallback=None)
                    return _parse_ini_number(v)
                except Exception:
                    return None

            # unidades (mm ou inch)
            units = "mm"
            try:
                units = str(cfg.get("TRAJ", "LINEAR_UNITS", fallback="mm")).strip().lower()
            except Exception:
                units = "mm"

            unit_to_mm = 25.4 if units.startswith("in") else 1.0

            # TRAJ (limite global)
            v_traj = _getf("TRAJ", "MAX_LINEAR_VELOCITY")
            if v_traj is None:
                v_traj = _getf("TRAJ", "MAX_VELOCITY")

            # eixos/joints (limite por eixo) — pega o maior e depois limita com TRAJ
            vx = _getf("AXIS_X", "MAX_VELOCITY")
            vz = _getf("AXIS_Z", "MAX_VELOCITY")
            vj0 = _getf("JOINT_0", "MAX_VELOCITY")
            vj1 = _getf("JOINT_1", "MAX_VELOCITY")

            axis_candidates = [v for v in (vx, vz, vj0, vj1) if isinstance(v, (int, float)) and v and v > 0.0]
            v_axis = max(axis_candidates) if axis_candidates else None

            # seleção final (conservadora)
            candidates_final = []
            if v_traj is not None and v_traj > 0.0:
                candidates_final.append(v_traj)
            if v_axis is not None and v_axis > 0.0:
                candidates_final.append(v_axis)

            if not candidates_final:
                # fallback opcional: DISPLAY (não é o "motion limit", mas ajuda em configs incompletas)
                v_disp = _getf("DISPLAY", "MAX_LINEAR_VELOCITY")
                if v_disp is not None and v_disp > 0.0:
                    v_mm_min = (v_disp * unit_to_mm) * 60.0
                    return float(v_mm_min) if v_mm_min > 1.0 else 0.0
                return 0.0

            if (v_traj is not None and v_traj > 0.0) and (v_axis is not None and v_axis > 0.0):
                v_units_per_s = min(v_traj, v_axis)
            else:
                v_units_per_s = max(candidates_final)

            v_mm_min = (v_units_per_s * unit_to_mm) * 60.0
            if v_mm_min <= 1.0:
                return 0.0

            return float(v_mm_min)

        except Exception:
            return 0.0


    def _sync_jog_speed_widgets(self, pct: int):
        """Sincroniza slider e spinbox do JOG sem loop."""
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
        """
        UI nova:
          - lbl_jog_tittle = texto fixo "VELOCIDADE JOG"
          - lbl_jog_value  = valor numérico "XXXX mm/min"
        Compatibilidade:
          - se lbl_jog_value não existir, atualiza lbl_jog_tittle no formato antigo.
        """
        try:
            vmax = float(getattr(self, "_jog_speed_max_mm_min", 1000.0))
            pct = int(getattr(self, "_jog_speed_pct", 100))
            vcur = vmax * (float(pct) / 100.0)

            # Preferência: UI nova (valor separado)
            if hasattr(self, "lbl_jog_value"):
                self.lbl_jog_value.setText(f"{vcur:.0f} mm/min")
                return

            # Fallback: UI antiga (título + valor no mesmo label)
            if hasattr(self, "lbl_jog_tittle"):
                self.lbl_jog_tittle.setText(f"VELOCIDADE JOG: {vcur:.0f} mm/min")
        except Exception:
            pass


    def _apply_jog_speed_pct(self, pct: int):
        """
        Aplica % de jog:
          - atualiza estado interno
          - atualiza título
          - tenta aplicar no HAL (se existir writer)
        """
        pct_i = int(max(0, min(120, int(pct))))
        self._jog_speed_pct = pct_i

        # UI: título e sync
        self._update_jog_speed_title()
        self._sync_jog_speed_widgets(pct_i)

        # valor absoluto (mm/min) para HAL (se disponível)
        try:
            vmax = float(getattr(self, "_jog_speed_max_mm_min", 1000.0))
            vcur = vmax * (float(pct_i) / 100.0)

            # tenta pinos típicos (sem quebrar se não existirem)
            try:
                import hal

                candidates = (
                    "halui.jog-speed",           # algumas configs
                    "halui.axis.jog-speed",      # algumas configs
                )

                for pin in candidates:
                    try:
                        if hasattr(hal, "pin_has_writer") and hal.pin_has_writer(pin):
                            hal.set_p(pin, str(float(vcur)))
                            break
                    except Exception:
                        pass
            except Exception:
                pass

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



if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    win = IceqMainWindow()
    win.show()
    sys.exit(app.exec_())
