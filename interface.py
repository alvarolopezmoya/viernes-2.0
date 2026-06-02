"""
interface.py — Interfaz visual HUD para JARVIS 2.0.

Framework: PyQt6 con efecto glassmorphism real en Windows 11 via DWM API.
Frameless, transparente, siempre al frente, draggable.

Nota DWM blur:
  La llamada a DwmSetWindowAttribute con DWMWA_SYSTEMBACKDROP_TYPE=2 (Acrylic)
  activa el backdrop blur nativo de Windows 11 22H2+.
  En Windows 10 o builds anteriores falla silenciosamente con un fallback
  de fondo semitransparente sólido.
"""
import ctypes
import math
import sqlite3
import time
from typing import Optional

import psutil
from PyQt6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    Qt,
    QTimer,
)
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPen,
)
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollBar,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from config import (
    ANIMATION_SLEEP_MS,
    ANIMATION_WAKE_MS,
    COLOR_ACCENT,
    COLOR_BG,
    COLOR_DANGER,
    COLOR_PRIMARY,
    COLOR_TEXT,
    COLOR_TEXT_DIM,
    FONT_FAMILY,
    FONT_SIZE_MAIN,
    FONT_SIZE_SMALL,
    WINDOW_CORNER_RADIUS,
    WINDOW_HEIGHT,
    WINDOW_OPACITY,
    WINDOW_WIDTH,
    WAVEFORM_BARS,
    WAVEFORM_COLOR_IDLE,
    WAVEFORM_COLOR_LISTEN,
    WAVEFORM_COLOR_SPEAK,
    WAVEFORM_FPS,
)

# Estados (deben coincidir con brain.py)
STATE_IDLE = "IDLE"
STATE_LISTENING = "LISTENING"
STATE_PROCESSING = "PROCESSING"
STATE_SPEAKING = "SPEAKING"


# ================================================================
# Glassmorphism via Windows DWM API
# ================================================================

def _apply_dwm_blur(hwnd: int) -> bool:
    """
    Aplica el efecto Acrylic blur de Windows 11 via DWM API.
    Retorna True si tuvo éxito, False si no está disponible (Windows 10, etc.).
    """
    try:
        DWMWA_SYSTEMBACKDROP_TYPE = 38
        DWMSBT_TRANSIENTWINDOW = 3  # Acrylic
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd,
            DWMWA_SYSTEMBACKDROP_TYPE,
            ctypes.byref(ctypes.c_int(DWMSBT_TRANSIENTWINDOW)),
            ctypes.sizeof(ctypes.c_int),
        )
        return True
    except Exception:
        return False


# ================================================================
# Waveform
# ================================================================

class WaveformWidget(QWidget):
    """
    Visualizador de audio en tiempo real con barras reactivas.
    Dibuja con QPainter a WAVEFORM_FPS fps usando QTimer.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(80)
        self.setMaximumHeight(80)
        self._bars: list[float] = [0.0] * WAVEFORM_BARS
        self._phase: float = 0.0
        self._state: str = STATE_IDLE

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._animate)
        self._timer.start(1000 // WAVEFORM_FPS)

    def update_waveform(self, data: list[float]) -> None:
        """Recibe datos de audio normalizados [0.0–1.0]."""
        if len(data) >= WAVEFORM_BARS:
            step = len(data) // WAVEFORM_BARS
            self._bars = [
                min(1.0, abs(data[i * step]))
                for i in range(WAVEFORM_BARS)
            ]
        self.update()

    def set_state(self, state: str) -> None:
        """Actualiza el estado visual del waveform."""
        self._state = state

    def _animate(self) -> None:
        """Animación sinusoidal idle cuando no hay audio real."""
        self._phase += 0.08
        if self._state == STATE_IDLE:
            self._bars = [
                0.15 + 0.1 * math.sin(self._phase + i * 0.3)
                for i in range(WAVEFORM_BARS)
            ]
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = self.width(), self.height()
        bar_width = w / WAVEFORM_BARS
        center_y = h / 2

        color_map = {
            STATE_IDLE: QColor(WAVEFORM_COLOR_IDLE),
            STATE_LISTENING: QColor(WAVEFORM_COLOR_LISTEN),
            STATE_PROCESSING: QColor(COLOR_PRIMARY),
            STATE_SPEAKING: QColor(WAVEFORM_COLOR_SPEAK),
        }
        base_color = color_map.get(self._state, QColor(WAVEFORM_COLOR_IDLE))

        for i, amplitude in enumerate(self._bars):
            bar_h = max(2.0, amplitude * (h - 8))
            x = i * bar_width + bar_width * 0.15
            bw = bar_width * 0.7

            gradient = QLinearGradient(x, center_y - bar_h / 2, x, center_y + bar_h / 2)
            gradient.setColorAt(0.0, base_color.lighter(150))
            gradient.setColorAt(0.5, base_color)
            gradient.setColorAt(1.0, base_color.darker(150))

            painter.setBrush(QBrush(gradient))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(
                int(x), int(center_y - bar_h / 2),
                int(bw), int(bar_h),
                2, 2,
            )


# ================================================================
# Chat display
# ================================================================

class ChatDisplay(QTextEdit):
    """Historial de conversación con estilo terminal."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(QFont(FONT_FAMILY, FONT_SIZE_MAIN))
        self.setStyleSheet(f"""
            QTextEdit {{
                background: transparent;
                color: {COLOR_TEXT};
                border: none;
                selection-background-color: {COLOR_PRIMARY};
            }}
            QScrollBar:vertical {{
                background: {COLOR_BG};
                width: 4px;
                border-radius: 2px;
            }}
            QScrollBar::handle:vertical {{
                background: {COLOR_PRIMARY};
                border-radius: 2px;
            }}
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {{
                height: 0px;
            }}
        """)

    def add_message(self, sender: str, text: str) -> None:
        """Añade un mensaje formateado con timestamp y color por emisor."""
        timestamp = time.strftime("%H:%M")
        color = COLOR_ACCENT if sender == "VIERNES" else COLOR_TEXT_DIM

        self.append(
            f'<span style="color:{COLOR_TEXT_DIM};">[{timestamp}]</span> '
            f'<span style="color:{color};font-weight:bold;">{sender}:</span> '
            f'<span style="color:{COLOR_TEXT};">{text}</span>'
        )
        self.verticalScrollBar().setValue(
            self.verticalScrollBar().maximum()
        )


# ================================================================
# Status bar
# ================================================================

class StatusBar(QLabel):
    """Barra inferior con métricas del sistema en tiempo real."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFont(QFont(FONT_FAMILY, FONT_SIZE_SMALL))
        self.setStyleSheet(f"""
            color: {COLOR_TEXT_DIM};
            padding: 4px 8px;
            border-top: 1px solid rgba(0,139,139,0.3);
        """)
        self._jarvis_state: str = STATE_IDLE
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._update)
        self._timer.start(2000)
        self._update()

    def set_jarvis_state(self, state: str) -> None:
        """Actualiza el estado mostrado."""
        self._jarvis_state = state
        self._update()

    def _update(self) -> None:
        cpu = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory()
        self.setText(
            f"CPU: {cpu:.0f}%  |  "
            f"RAM: {ram.percent:.0f}% ({ram.used / 1e9:.1f}GB)  |  "
            f"FRIDAY: {self._jarvis_state}"
        )


# ================================================================
# Stats Panel — barra de progreso minimalista
# ================================================================

class _MiniBar(QWidget):
    """Barra de progreso de 6px de alto para métricas del sistema."""

    def __init__(self, color: str, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(6)
        self._value: float = 0.0
        self._color = QColor(color)

    def set_value(self, v: float) -> None:
        self._value = max(0.0, min(1.0, v))
        self.update()

    def paintEvent(self, _) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        # Fondo
        painter.setBrush(QBrush(QColor(20, 40, 40)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(0, 0, w, h, 3, 3)
        # Relleno
        fill_w = int(w * self._value)
        if fill_w > 4:
            # Gradiente horizontal
            grad = QLinearGradient(0, 0, fill_w, 0)
            grad.setColorAt(0.0, self._color.darker(120))
            grad.setColorAt(1.0, self._color.lighter(140))
            painter.setBrush(QBrush(grad))
            painter.drawRoundedRect(0, 0, fill_w, h, 3, 3)


# ================================================================
# Panel lateral — Estadísticas + Historial
# ================================================================

# Nombres legibles para los intents más comunes
_INTENT_LABELS: dict[str, str] = {
    "play_song": "Canción",
    "web_search": "Búsqueda web",
    "open_app": "Abrir app",
    "open_file": "Abrir archivo",
    "get_system_stats": "Estado sistema",
    "activate_work_mode": "Modo trabajo",
    "open_vscode": "VS Code",
    "open_terminal": "Terminal",
    "screenshot": "Captura",
    "volume_up": "Subir volumen",
    "volume_down": "Bajar volumen",
    "translate_clipboard": "Traducir",
    "correct_clipboard": "Corregir",
    "read_clipboard": "Portapapeles",
    "search_memory": "Memoria",
    "read_notifications": "Notificaciones",
    "close_distractions": "Modo enfoque",
    "minimize_all": "Minimizar",
    "small_talk": "Charla",
    "play_genre": "Género musical",
    "now_playing": "Qué suena",
    "conversation": "Conversación",
}


class StatsPanel(QFrame):
    """
    Panel lateral colapsable con estadísticas en tiempo real:
      · Barras CPU / RAM
      · Uptime de sesión y total de comandos
      · Top 5 intents de hoy (desde SQLite)
      · Historial de las últimas 8 tareas
    """

    _PANEL_WIDTH = 215

    def __init__(self, db_path, parent=None) -> None:
        super().__init__(parent)
        self._db_path = str(db_path)
        self._expanded: bool = False
        self._session_start: float = time.time()
        self._cmd_count: int = 0
        self._anim: Optional[QPropertyAnimation] = None
        self._tasks: list[QLabel] = []

        self.setMaximumWidth(0)
        self.setStyleSheet(f"""
            QFrame {{
                background: rgba(8, 18, 18, 0.97);
                border-left: 1px solid rgba(0, 139, 139, 0.35);
            }}
        """)

        self._build_ui()

        # Timer de actualización cada 2s
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(2000)

    # ── Construcción ─────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(6)

        def section_title(text: str) -> QLabel:
            lbl = QLabel(text)
            lbl.setFont(QFont(FONT_FAMILY, 7, QFont.Weight.Bold))
            lbl.setStyleSheet(f"color: {COLOR_PRIMARY}; letter-spacing: 2px; padding: 0;")
            return lbl

        def metric_label() -> QLabel:
            lbl = QLabel("—")
            lbl.setFont(QFont(FONT_FAMILY, FONT_SIZE_SMALL))
            lbl.setStyleSheet(f"color: {COLOR_TEXT}; padding: 0;")
            return lbl

        # ── Sección: SISTEMA ────────────────────────────────────────
        root.addWidget(section_title("SISTEMA"))
        root.addWidget(self._sep())

        self._cpu_label = metric_label()
        self._cpu_bar   = _MiniBar(COLOR_ACCENT)
        self._ram_label = metric_label()
        self._ram_bar   = _MiniBar(COLOR_PRIMARY)

        root.addWidget(self._row("CPU", self._cpu_label))
        root.addWidget(self._cpu_bar)
        root.addWidget(self._row("RAM", self._ram_label))
        root.addWidget(self._ram_bar)

        root.addSpacing(4)

        # ── Sección: SESIÓN ─────────────────────────────────────────
        root.addWidget(section_title("SESIÓN"))
        root.addWidget(self._sep())

        self._uptime_label = metric_label()
        self._cmds_label   = metric_label()

        root.addWidget(self._row("⏱", self._uptime_label))
        root.addWidget(self._row("💬", self._cmds_label))

        root.addSpacing(4)

        # ── Sección: TOP HOY ────────────────────────────────────────
        root.addWidget(section_title("TOP HOY"))
        root.addWidget(self._sep())

        self._top_labels: list[QLabel] = []
        for _ in range(5):
            lbl = QLabel("")
            lbl.setFont(QFont(FONT_FAMILY, FONT_SIZE_SMALL))
            lbl.setStyleSheet(f"color: {COLOR_TEXT_DIM}; padding: 1px 0;")
            lbl.setWordWrap(False)
            root.addWidget(lbl)
            self._top_labels.append(lbl)

        root.addSpacing(4)

        # ── Sección: RECIENTES ──────────────────────────────────────
        root.addWidget(section_title("RECIENTES"))
        root.addWidget(self._sep())

        self._task_layout = QVBoxLayout()
        self._task_layout.setSpacing(1)
        root.addLayout(self._task_layout)

        root.addStretch()

    def _sep(self) -> QFrame:
        """Línea separadora horizontal."""
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color: rgba(0,139,139,0.25); margin: 0;")
        line.setFixedHeight(1)
        return line

    def _row(self, key: str, value_label: QLabel) -> QWidget:
        """Fila clave–valor en una línea."""
        row = QWidget()
        hl = QHBoxLayout(row)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(4)
        key_lbl = QLabel(key)
        key_lbl.setFont(QFont(FONT_FAMILY, FONT_SIZE_SMALL))
        key_lbl.setStyleSheet(f"color: {COLOR_TEXT_DIM}; padding: 0;")
        key_lbl.setFixedWidth(28)
        hl.addWidget(key_lbl)
        hl.addWidget(value_label, stretch=1)
        return row

    # ── Actualización de datos ────────────────────────────────────────

    def _refresh(self) -> None:
        """Actualiza todas las métricas (llamado por QTimer cada 2s)."""
        # CPU / RAM
        cpu = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory()
        self._cpu_label.setText(f"{cpu:.0f}%")
        self._cpu_bar.set_value(cpu / 100)
        self._ram_label.setText(
            f"{ram.percent:.0f}%  {ram.used / 1e9:.1f}GB"
        )
        self._ram_bar.set_value(ram.percent / 100)

        # Uptime
        elapsed = int(time.time() - self._session_start)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        self._uptime_label.setText(f"{h:02d}:{m:02d}:{s:02d}")

        # Estadísticas de DB (lectura rápida, solo HOY)
        self._refresh_db_stats()

    def _refresh_db_stats(self) -> None:
        """Lee SQLite para contar comandos y top intents de hoy."""
        try:
            conn = sqlite3.connect(self._db_path, timeout=3)
            conn.execute("PRAGMA busy_timeout=3000")
            # Total comandos hoy
            row = conn.execute(
                "SELECT COUNT(*) FROM conversations "
                "WHERE date(timestamp) = date('now')"
            ).fetchone()
            total = row[0] if row else 0
            self._cmds_label.setText(f"{total} hoy")

            # Top 5 intents hoy
            rows = conn.execute(
                "SELECT intent_type, COUNT(*) as cnt "
                "FROM conversations "
                "WHERE date(timestamp) = date('now') "
                "  AND intent_type IS NOT NULL "
                "  AND intent_type != '' "
                "GROUP BY intent_type "
                "ORDER BY cnt DESC LIMIT 5"
            ).fetchall()
            conn.close()

            for i, lbl in enumerate(self._top_labels):
                if i < len(rows):
                    intent, cnt = rows[i]
                    name = _INTENT_LABELS.get(intent, intent.replace("_", " "))
                    # Truncar para que quepa en 215px
                    name = name[:16]
                    lbl.setText(f"▸ {name:<16} ×{cnt}")
                    lbl.setStyleSheet(
                        f"color: {'#00dddd' if i == 0 else COLOR_TEXT_DIM}; padding: 1px 0;"
                    )
                else:
                    lbl.setText("")
        except Exception:
            pass  # DB bloqueada o inexistente — silencioso

    # ── API pública ───────────────────────────────────────────────────

    def add_task(self, task: str) -> None:
        """Añade una tarea al historial (máximo 8, LIFO)."""
        lbl = QLabel(f"▸ {task[:22]}")
        lbl.setFont(QFont(FONT_FAMILY, FONT_SIZE_SMALL))
        lbl.setStyleSheet(f"color: {COLOR_TEXT}; padding: 1px 0;")
        self._task_layout.insertWidget(0, lbl)
        self._tasks.append(lbl)
        if len(self._tasks) > 8:
            old = self._tasks.pop(0)
            old.deleteLater()
        self._refresh_db_stats()  # Actualizar contadores inmediatamente

    def toggle(self) -> None:
        """Expande o colapsa el panel con animación."""
        self._expanded = not self._expanded
        if self._expanded:
            self._refresh()  # Datos frescos al abrir
        self._anim = QPropertyAnimation(self, b"maximumWidth")
        self._anim.setDuration(260)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.setStartValue(self.maximumWidth())
        self._anim.setEndValue(self._PANEL_WIDTH if self._expanded else 0)
        self._anim.start()


# ================================================================
# Ventana principal
# ================================================================

class JARVISInterface(QMainWindow):
    """
    Ventana principal de JARVIS: HUD frameless con glassmorphism real.
    Siempre al frente, draggable, inicia oculta.
    """

    def __init__(self) -> None:
        super().__init__()
        self._drag_pos: Optional[QPoint] = None
        self._setup_window()
        self._build_ui()
        self._setup_fade_animations()

        # Iniciar completamente oculta
        self.setWindowOpacity(0.0)
        self.hide()

    # ----------------------------------------------------------------
    # Configuración de ventana
    # ----------------------------------------------------------------

    def _setup_window(self) -> None:
        """Configura ventana frameless y transparente, posicionada a la derecha."""
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.resize(WINDOW_WIDTH, WINDOW_HEIGHT)

        # Posicionar en borde derecho de la pantalla principal
        screen = QApplication.primaryScreen().geometry()
        x = screen.width() - WINDOW_WIDTH - 40
        y = (screen.height() - WINDOW_HEIGHT) // 2
        self.move(x, y)

    def showEvent(self, event) -> None:
        """Aplica DWM blur cuando la ventana es visible por primera vez."""
        super().showEvent(event)
        _apply_dwm_blur(int(self.winId()))

    # ----------------------------------------------------------------
    # Construcción de UI
    # ----------------------------------------------------------------

    def _build_ui(self) -> None:
        """Construye todos los componentes visuales."""
        central = QWidget()
        # Fondo semitransparente sólido — DWM añade blur encima en Win11
        central.setStyleSheet(f"""
            QWidget {{
                background: rgba(10, 15, 15, {int(WINDOW_OPACITY * 230)});
                border-radius: {WINDOW_CORNER_RADIUS}px;
                border: 1px solid rgba(0, 139, 139, 0.6);
            }}
        """)
        self.setCentralWidget(central)

        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Crear side_panel antes que el header (el header referencia self.side_panel)
        from config import DB_PATH
        self.side_panel = StatsPanel(db_path=DB_PATH)

        # Panel de contenido principal
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(12, 12, 12, 8)
        content_layout.setSpacing(8)

        content_layout.addWidget(self._build_header())

        self.waveform = WaveformWidget()
        content_layout.addWidget(self.waveform)

        self.chat = ChatDisplay()
        content_layout.addWidget(self.chat, stretch=1)

        self.status_bar = StatusBar()
        content_layout.addWidget(self.status_bar)

        main_layout.addWidget(content, stretch=1)
        main_layout.addWidget(self.side_panel)

    def _build_header(self) -> QWidget:
        """Header con logo, nombre y botones de control."""
        header = QWidget()
        layout = QHBoxLayout(header)
        layout.setContentsMargins(0, 0, 0, 4)

        title = QLabel("◈ Friday")
        title.setFont(QFont(FONT_FAMILY, 14, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {COLOR_ACCENT}; letter-spacing: 3px;")

        toggle_btn = QPushButton("≡")
        toggle_btn.setFixedSize(28, 28)
        toggle_btn.setStyleSheet(f"""
            QPushButton {{
                color: {COLOR_PRIMARY}; background: transparent;
                border: 1px solid {COLOR_PRIMARY}; border-radius: 4px;
                font-size: 14px;
            }}
            QPushButton:hover {{ background: rgba(0,139,139,0.2); }}
        """)
        toggle_btn.clicked.connect(self.side_panel.toggle)

        close_btn = QPushButton("×")
        close_btn.setFixedSize(28, 28)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                color: {COLOR_DANGER}; background: transparent;
                border: 1px solid {COLOR_DANGER}; border-radius: 4px;
                font-size: 16px;
            }}
            QPushButton:hover {{ background: rgba(255,68,68,0.2); }}
        """)
        close_btn.clicked.connect(self.go_to_sleep)

        layout.addWidget(title)
        layout.addStretch()
        layout.addWidget(toggle_btn)
        layout.addWidget(close_btn)
        return header

    # ----------------------------------------------------------------
    # Animaciones fade
    # ----------------------------------------------------------------

    def _setup_fade_animations(self) -> None:
        """Prepara las animaciones de fade-in y fade-out."""
        self._fade_in = QPropertyAnimation(self, b"windowOpacity")
        self._fade_in.setDuration(ANIMATION_WAKE_MS)
        self._fade_in.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade_in.setStartValue(0.0)
        self._fade_in.setEndValue(WINDOW_OPACITY)

        self._fade_out = QPropertyAnimation(self, b"windowOpacity")
        self._fade_out.setDuration(ANIMATION_SLEEP_MS)
        self._fade_out.setEasingCurve(QEasingCurve.Type.InCubic)
        self._fade_out.setStartValue(WINDOW_OPACITY)
        self._fade_out.setEndValue(0.0)
        self._fade_out.finished.connect(self.hide)

    # ----------------------------------------------------------------
    # API Pública
    # ----------------------------------------------------------------

    def wake_up(self) -> None:
        """Muestra la interfaz con animación fade-in."""
        self.show()
        self._fade_out.stop()
        self._fade_in.start()

    def go_to_sleep(self) -> None:
        """Oculta la interfaz con animación fade-out."""
        self._fade_in.stop()
        self._fade_out.start()

    def update_waveform(self, audio_data: list[float]) -> None:
        """Actualiza las barras del waveform con nuevos datos de audio."""
        self.waveform.update_waveform(audio_data)

    def display_message(self, sender: str, text: str) -> None:
        """Muestra un mensaje en el chat display."""
        self.chat.add_message(sender, text)

    def set_status(self, status: str) -> None:
        """Actualiza el estado: IDLE | LISTENING | PROCESSING | SPEAKING."""
        self.status_bar.set_jarvis_state(status)
        self.waveform.set_state(status)

    def add_task_to_panel(self, task: str) -> None:
        """Registra una tarea ejecutada en el panel lateral."""
        self.side_panel.add_task(task)

    # ----------------------------------------------------------------
    # Drag para mover ventana sin bordes
    # ----------------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_pos and event.buttons() == Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_pos = None


# ----------------------------------------------------------------
# Test standalone
# ----------------------------------------------------------------

if __name__ == "__main__":
    import sys

    app = QApplication(sys.argv)
    win = JARVISInterface()
    win.wake_up()
    win.display_message("VIERNES", "Sistema inicializado. Buenos días, Señor.")
    win.display_message("Tú", "Abre el editor.")
    win.set_status(STATE_LISTENING)
    sys.exit(app.exec())
