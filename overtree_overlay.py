import sys
import os
import configparser
import threading
import logging

# Debug log — пишем в %APPDATA%\overtree.log
_log_path = os.path.join(os.getenv("APPDATA", "."), "overtree.log")
logging.basicConfig(
    filename=_log_path, level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
)
log = logging.getLogger("overtree")

def _excepthook(exc_type, exc_value, exc_tb):
    log.error("UNCAUGHT EXCEPTION", exc_info=(exc_type, exc_value, exc_tb))
    sys.__excepthook__(exc_type, exc_value, exc_tb)
sys.excepthook = _excepthook

if sys.platform == "win32":
    import win32pipe
    import win32file

from PyQt6.QtCore import Qt, QPoint, QTimer, QObject, pyqtSignal, QVariantAnimation, QEasingCurve, QThread
from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
                             QLabel, QListWidget, QListWidgetItem, QLineEdit,
                             QGraphicsDropShadowEffect, QGraphicsBlurEffect,
                             QPushButton, QMenu, QSizeGrip)
from PyQt6.QtGui import QFont, QColor, QAction, QPainter, QRadialGradient, QCursor


class PipeCommBridge(QObject):
    path_received = pyqtSignal(str)


class ScanWorker(QThread):
    result_ready = pyqtSignal(list, int)
    batch_ready = pyqtSignal(list, int, bool)

    def __init__(self, target_path, max_depth, count_files):
        super().__init__()
        self.target_path = target_path
        self.max_depth = max_depth
        self.count_files = count_files
        self.total_folders = 0
        self._cancelled = False
        self._lock = threading.Lock()

    def cancel(self):
        with self._lock:
            self._cancelled = True

    def is_cancelled(self):
        with self._lock:
            return self._cancelled

    def count_files_in_dir(self, dir_path):
        if not self.count_files:
            return 0
        try:
            with os.scandir(dir_path) as entries:
                return sum(1 for entry in entries if entry.is_file(follow_symlinks=False))
        except Exception:
            return 0

    def run(self):
        try:
            batch = []
            batch_size = 50
            self.scan_folder(self.target_path, "", 1, batch, batch_size)
            if not self.is_cancelled() and batch:
                self.batch_ready.emit(batch, self.total_folders, True)
            if not self.is_cancelled():
                self.result_ready.emit([], self.total_folders)
        except Exception:
            log.error("worker.run CRASH", exc_info=True)

    def scan_folder(self, folder_path, prefix, current_depth, batch, batch_size):
        if current_depth > self.max_depth or self.is_cancelled():
            return
        try:
            subfolders = []
            with os.scandir(folder_path) as entries:
                for entry in entries:
                    if self.is_cancelled():
                        return
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            name = entry.name
                            name_upper = name.upper()
                            if (name.startswith('.') or name.startswith('$')
                                    or "SYSTEM VOLUME INFORMATION" in name_upper
                                    or "RECOVERY" in name_upper):
                                continue
                            subfolders.append(name)
                    except Exception:
                        continue
            subfolders.sort(key=str.lower)
        except Exception:
            return

        total = len(subfolders)
        self.total_folders += total

        for index, name in enumerate(subfolders):
            if self.is_cancelled():
                return
            is_last = (index == total - 1)
            marker = "└── " if is_last else "├── "
            full_path = os.path.join(folder_path, name)

            f_count = self.count_files_in_dir(full_path)
            files_text = f" [{f_count} f]" if self.count_files else ""

            display_line = f"{prefix}{marker}📁 {name}{files_text}"
            batch.append((display_line, full_path, current_depth))

            if len(batch) >= batch_size:
                self.batch_ready.emit(list(batch), self.total_folders, False)
                batch.clear()

            self.scan_folder(full_path, prefix + ("    " if is_last else "│   "), current_depth + 1, batch, batch_size)


class FolderTreeOverlay(QWidget):
    def __init__(self, bridge_obj):
        super().__init__()
        self.bridge = bridge_obj
        self.bridge.path_received.connect(self.show_for_path, Qt.ConnectionType.QueuedConnection)

        self.target_path = ""
        self.shadow_blur = 15
        self.max_dots_count = 5
        self.top_buttons = []
        self.bottom_buttons = []

        self.worker = None
        self.radar_radius = 0
        self._last_dots_state = None
        self.path_map = {}
        self._all_tree_items = []

        self.ini_path = os.path.join(os.getenv("APPDATA"), "overtree.ini")
        self.max_depth, self.auto_close, self.count_files, self.follow_mode, self.search_enabled, self.win_w, self.win_h = self.load_settings()

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setMinimumSize(410 + self.shadow_blur * 2, 290 + self.shadow_blur * 2)
        self.resize(self.win_w, self.win_h)

        self.init_ui()

        self.radar_anim = QVariantAnimation(self)
        self.radar_anim.setDuration(400)
        self.radar_anim.setStartValue(0)
        self.radar_anim.setEndValue(300)
        self.radar_anim.setEasingCurve(QEasingCurve.Type.OutQuad)
        self.radar_anim.valueChanged.connect(self.animate_radar)

        self.close_timer = QTimer(self)
        self.close_timer.setInterval(3500)
        self.close_timer.setSingleShot(True)
        self.close_timer.timeout.connect(self.hide_overlay)

        self.search_input.setVisible(self.search_enabled)

    def animate_radar(self, value):
        self.radar_radius = value
        self.update()

    def show_for_path(self, new_path):
        if not new_path or not os.path.exists(new_path):
            return

        self.target_path = new_path
        if not self.isVisible():
            screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
            screen_rect = screen.geometry()
            self.move(int((screen_rect.width() - self.width()) / 2) + screen_rect.x(),
                      int((screen_rect.height() - self.height()) / 2) + screen_rect.y())

        self.update_tree()
        self.scroll_bar.setValue(0)
        self.show()
        self.raise_()
        self.activateWindow()

        if self.auto_close and not self.follow_mode:
            self.close_timer.start()

    def hide_overlay(self):
        self.close_timer.stop()
        self._cancel_worker()
        self.hide()

    def _cancel_worker(self):
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait(2000)

    def enterEvent(self, event):
        if self.auto_close and not self.follow_mode:
            self.close_timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event):
        if self.auto_close and not self.follow_mode and not self.isActiveWindow():
            self.close_timer.start()
        super().leaveEvent(event)

    def focusOutEvent(self, event):
        if self.auto_close and not self.follow_mode and not self.underMouse():
            self.close_timer.start()
        super().focusOutEvent(event)

    def load_settings(self):
        config = configparser.ConfigParser()
        default_w = 410 + self.shadow_blur * 2
        default_h = 290 + self.shadow_blur * 2
        if os.path.exists(self.ini_path):
            try:
                config.read(self.ini_path)
                return (config.getint("General", "Depth", fallback=2),
                        config.getboolean("General", "AutoClose", fallback=True),
                        config.getboolean("General", "CountFiles", fallback=False),
                        config.getboolean("General", "FollowMode", fallback=False),
                        config.getboolean("General", "Search", fallback=False),
                        config.getint("General", "Width", fallback=default_w),
                        config.getint("General", "Height", fallback=default_h))
            except Exception:
                return 2, True, False, False, False, default_w, default_h
        return 2, True, False, False, False, default_w, default_h

    def save_settings(self):
        config = configparser.ConfigParser()
        config["General"] = {
            "Depth": str(self.max_depth),
            "AutoClose": str(self.auto_close),
            "CountFiles": str(self.count_files),
            "FollowMode": str(self.follow_mode),
            "Search": str(self.search_enabled),
            "Width": str(self.width()),
            "Height": str(self.height())
        }
        try:
            with open(self.ini_path, "w") as configfile:
                config.write(configfile)
        except Exception:
            pass

    def init_ui(self):
        self.blur_card = QWidget(self)
        self.blur_card.setGeometry(self.shadow_blur, self.shadow_blur, 410, 290)
        self.blur_card.setStyleSheet("background-color: rgba(15, 18, 26, 0.75); border-radius: 16px;")
        blur_effect = QGraphicsBlurEffect()
        blur_effect.setBlurRadius(10)
        self.blur_card.setGraphicsEffect(blur_effect)

        self.card = QWidget(self)
        self.card.setGeometry(self.shadow_blur, self.shadow_blur, 410, 290)
        self.card.setObjectName("MainCard")
        self.card.setStyleSheet("QWidget#MainCard { border: 1px solid rgba(51, 255, 243, 0.2); border-radius: 16px; background-color: transparent; }")

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(self.shadow_blur)
        shadow.setColor(QColor(0, 0, 0, 180))
        shadow.setOffset(0, 4)
        self.card.setGraphicsEffect(shadow)

        main_layout = QVBoxLayout(self.card)
        main_layout.setContentsMargins(25, 10, 25, 10)
        main_layout.setSpacing(2)

        top_layout = QHBoxLayout()
        lbl_hint = QLabel("(Esc - скрыть. Двойной клик — перейти)")
        lbl_hint.setFont(QFont("Segoe UI", 7))
        lbl_hint.setStyleSheet("color: #666666; background: transparent; border: none;")
        top_layout.addWidget(lbl_hint)
        top_layout.addStretch()

        self.settings_btn = QPushButton("⚙", self.card)
        self.settings_btn.setFont(QFont("Segoe UI", 11))
        self.settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_btn.setStyleSheet("QPushButton { color: #555555; background: transparent; border: none; padding: 0px; } QPushButton::hover { color: #33FFF3; }")
        self.settings_btn.clicked.connect(self.show_settings_menu)
        top_layout.addWidget(self.settings_btn)
        main_layout.addLayout(top_layout)

        self.top_dots_layout = QHBoxLayout()
        self.top_dots_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.top_dots_layout.setSpacing(6)
        main_layout.addLayout(self.top_dots_layout)

        dot_style = "QPushButton { background-color: #33FFF3; border-radius: 2px; border: none; min-width: 4px; max-width: 4px; min-height: 4px; max-height: 4px; } QPushButton::hover { background-color: #00FF99; }"

        for i in range(self.max_dots_count):
            btn = QPushButton(self.card)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(dot_style)
            btn.clicked.connect(lambda checked, pct=(i + 1) / self.max_dots_count: self.scroll_to_percent(pct))
            self.top_dots_layout.addWidget(btn)
            self.top_buttons.append(btn)

        self.tree_list = QListWidget()
        self.tree_list.setFont(QFont("Consolas", 9))
        self.tree_list.setMinimumHeight(100)
        self.tree_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tree_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.tree_list.setStyleSheet("""
            QListWidget { background: transparent; border: none; }
            QListWidget::item { padding: 2px 0px; }
            QListWidget::item:hover { background: rgba(0, 255, 153, 0.08); border-radius: 3px; }
            QListWidget::item:selected { background: rgba(51, 255, 243, 0.15); color: #33FFF3; border-radius: 3px; }
            QScrollBar:vertical, QScrollBar:horizontal { width: 0px; height: 0px; background: transparent; }
        """)
        self.tree_list.setUniformItemSizes(True)
        self.tree_list.itemDoubleClicked.connect(self.on_item_double_clicked)
        main_layout.addWidget(self.tree_list)

        self.bottom_dots_layout = QHBoxLayout()
        self.bottom_dots_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.bottom_dots_layout.setSpacing(6)
        main_layout.addLayout(self.bottom_dots_layout)

        for i in range(self.max_dots_count):
            btn = QPushButton(self.card)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(dot_style)
            btn.clicked.connect(lambda checked: self.scroll_to_bottom())
            self.bottom_dots_layout.addWidget(btn)
            self.bottom_buttons.append(btn)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("🔍 Поиск папки...")
        self.search_input.setFont(QFont("Segoe UI", 9))
        self.search_input.setStyleSheet("""
            QLineEdit { background: rgba(255, 255, 255, 0.05); border: 1px solid rgba(51, 255, 243, 0.2);
                        border-radius: 6px; color: #00FF99; padding: 4px 8px; }
            QLineEdit:focus { border: 1px solid rgba(51, 255, 243, 0.5); }
        """)
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._on_search_changed)
        self.search_input.returnPressed.connect(self._on_search_return)
        self.search_input.setVisible(False)
        main_layout.addWidget(self.search_input)

        self._all_tree_items = []

        self.size_grip = QSizeGrip(self)
        self.size_grip.setStyleSheet("QSizeGrip { background: transparent; width: 14px; height: 14px; }")
        self.size_grip.setFixedSize(14, 14)

        self.resize_dot = QLabel(self)
        self.resize_dot.setFixedSize(8, 8)
        self.resize_dot.setStyleSheet("background-color: #33FFF3; border-radius: 4px;")
        self.resize_dot.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        self._save_size_timer = QTimer(self)
        self._save_size_timer.setSingleShot(True)
        self._save_size_timer.setInterval(500)
        self._save_size_timer.timeout.connect(self.save_settings)

        self.scroll_bar = self.tree_list.verticalScrollBar()
        self.scroll_bar.valueChanged.connect(self._on_scroll_changed)
        self.scroll_bar.rangeChanged.connect(self._on_scroll_changed)

    def _on_scroll_changed(self):
        current = self.scroll_bar.value()
        maximum = self.scroll_bar.maximum()
        state = (current, maximum)
        if state != self._last_dots_state:
            self._last_dots_state = state
            self._update_dots(current, maximum)

    def _update_dots(self, current_scroll, max_scroll):
        visible_style = "QPushButton { background-color: #33FFF3; border-radius: 2px; border: none; min-width: 4px; max-width: 4px; min-height: 4px; max-height: 4px; } QPushButton::hover { background-color: #00FF99; }"
        hidden_style = "QPushButton { color: transparent; background: transparent; border: none; }"

        if max_scroll > 0:
            scroll_percent = current_scroll / max_scroll
            top_count = round(scroll_percent * self.max_dots_count)
            bottom_count = self.max_dots_count - top_count

            for idx, btn in enumerate(self.top_buttons):
                if idx < top_count:
                    btn.setVisible(True); btn.setEnabled(True); btn.setStyleSheet(visible_style)
                else:
                    btn.setVisible(False); btn.setEnabled(False); btn.setStyleSheet(hidden_style)

            for idx, btn in enumerate(self.bottom_buttons):
                if idx < bottom_count:
                    btn.setVisible(True); btn.setEnabled(True); btn.setStyleSheet(visible_style)
                    try:
                        btn.clicked.disconnect()
                    except Exception:
                        pass
                    btn.clicked.connect(lambda checked, pct=(top_count + idx) / self.max_dots_count: self.scroll_to_percent(pct))
                else:
                    btn.setVisible(False); btn.setEnabled(False); btn.setStyleSheet(hidden_style)
        else:
            for btn in self.top_buttons + self.bottom_buttons:
                btn.setVisible(False); btn.setEnabled(False); btn.setStyleSheet(hidden_style)

    def resizeEvent(self, event):
        s = self.shadow_blur
        w = self.width() - s * 2
        h = self.height() - s * 2
        self.blur_card.setGeometry(s, s, w, h)
        self.card.setGeometry(s, s, w, h)
        self.size_grip.move(self.width() - 16, self.height() - 16)
        self.resize_dot.move(self.width() - 12, self.height() - 12)
        self._save_size_timer.start()
        super().resizeEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)

        if 0 < self.radar_radius < 300:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            center_x = self.width() / 2
            center_y = self.height() / 2

            alpha = int(100 * (1.0 - (self.radar_radius / 300.0)))
            gradient = QRadialGradient(center_x, center_y, self.radar_radius)
            gradient.setColorAt(0.0, QColor(51, 255, 243, 0))
            gradient.setColorAt(0.8, QColor(51, 255, 243, int(alpha * 0.3)))
            gradient.setColorAt(1.0, QColor(51, 255, 243, alpha))

            painter.setBrush(gradient)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QPoint(int(center_x), int(center_y)), int(self.radar_radius), int(self.radar_radius))
            painter.end()

    def scroll_to_percent(self, percent):
        max_scroll = self.scroll_bar.maximum()
        self.scroll_bar.setValue(int(max_scroll * percent))

    def scroll_to_bottom(self):
        self.scroll_bar.setValue(self.scroll_bar.maximum())

    def show_settings_menu(self):
        self.close_timer.stop()
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu { background-color: #12151C; color: #00FF99; border: 1px solid rgba(51, 255, 243, 0.3); border-radius: 6px; padding: 4px; }
            QMenu::item { padding: 4px 20px 4px 20px; background: transparent; }
            QMenu::item:selected { background-color: rgba(51, 255, 243, 0.15); color: #33FFF3; border-radius: 3px; }
            QMenu::item:checked { color: #33FFF3; font-weight: bold; }
            QMenu::separator { height: 1px; background: rgba(51, 255, 243, 0.15); margin: 4px 0px; }
        """)

        for i in range(1, 6):
            action = QAction(f"Глубина: {i}", menu)
            action.setCheckable(True)
            if i == self.max_depth:
                action.setChecked(True)
            action.triggered.connect(lambda checked, depth=i: self.change_depth(depth))
            menu.addAction(action)

        menu.addSeparator()

        autoclose_action = QAction("Автозакрытие", menu)
        autoclose_action.setCheckable(True)
        autoclose_action.setChecked(self.auto_close)
        autoclose_action.triggered.connect(self.toggle_autoclose)
        menu.addAction(autoclose_action)

        countfiles_action = QAction("Считать файлы", menu)
        countfiles_action.setCheckable(True)
        countfiles_action.setChecked(self.count_files)
        countfiles_action.triggered.connect(self.toggle_countfiles)
        menu.addAction(countfiles_action)

        follow_action = QAction("Режим Follow", menu)
        follow_action.setCheckable(True)
        follow_action.setChecked(self.follow_mode)
        follow_action.triggered.connect(self.toggle_followmode)
        menu.addAction(follow_action)

        search_action = QAction("Поиск", menu)
        search_action.setCheckable(True)
        search_action.setChecked(self.search_enabled)
        search_action.triggered.connect(self.toggle_search)
        menu.addAction(search_action)

        menu.addSeparator()

        exit_action = QAction("Выход (Закрыть процесс)", menu)
        exit_action.triggered.connect(QApplication.quit)
        menu.addAction(exit_action)

        menu.exec(self.settings_btn.mapToGlobal(QPoint(0, self.settings_btn.height())))

        if self.auto_close and not self.underMouse() and not self.follow_mode:
            self.close_timer.start()

    def change_depth(self, depth):
        self.max_depth = depth
        self.save_settings()
        self.update_tree()

    def toggle_autoclose(self, checked):
        self.auto_close = checked
        self.save_settings()
        if self.auto_close and not self.follow_mode:
            if not self.underMouse():
                self.close_timer.start()
        else:
            self.close_timer.stop()

    def toggle_countfiles(self, checked):
        self.count_files = checked
        self.save_settings()
        self.update_tree()

    def toggle_followmode(self, checked):
        self.follow_mode = checked
        self.save_settings()
        if self.follow_mode:
            self.close_timer.stop()
        else:
            if self.auto_close and not self.underMouse():
                self.close_timer.start()
        self.update_tree()

    def toggle_search(self, checked):
        self.search_enabled = checked
        self.save_settings()
        self.search_input.setVisible(checked)
        if not checked:
            self.search_input.clear()

    @staticmethod
    def _get_depth_color(depth):
        colors = [
            QColor(102, 255, 178),   # 1 — мягкий зелёный
            QColor(120, 230, 120),   # 2 — салатовый
            QColor(200, 220, 100),   # 3 — жёлто-зелёный
            QColor(240, 200, 80),    # 4 — тёплый жёлтый
            QColor(240, 170, 80),    # 5 — оранжевый
            QColor(220, 140, 110),   # 6 — персиковый
            QColor(200, 120, 160),   # 7 — розово-сиреневый
            QColor(170, 130, 200),   # 8 — мягкий фиолетовый
        ]
        idx = min(depth - 1, len(colors) - 1)
        return colors[idx]

    def _on_search_changed(self, text):
        text_lower = text.lower().strip()
        self.tree_list.setUpdatesEnabled(False)
        self.tree_list.clear()
        self.path_map = {}

        if not text_lower:
            for display_line, full_path, depth in self._all_tree_items:
                item = QListWidgetItem(display_line)
                item.setData(Qt.ItemDataRole.ForegroundRole, self._get_depth_color(depth))
                self.tree_list.addItem(item)
                self.path_map[display_line] = full_path
        else:
            seen_paths = set()
            all_items = self._all_tree_items
            for idx, (display_line, full_path, depth) in enumerate(all_items):
                if text_lower not in display_line.lower():
                    continue

                ancestors = []
                check_depth = depth - 1
                for i in range(idx - 1, -1, -1):
                    anc_line, anc_path, anc_depth = all_items[i]
                    if anc_depth == check_depth and full_path.startswith(anc_path + os.sep):
                        ancestors.insert(0, (anc_line, anc_path, anc_depth))
                        check_depth -= 1
                        if check_depth < 1:
                            break

                for anc_line, anc_path, anc_depth in ancestors:
                    if anc_path not in seen_paths:
                        seen_paths.add(anc_path)
                        anc_item = QListWidgetItem(anc_line)
                        anc_item.setData(Qt.ItemDataRole.ForegroundRole, self._get_depth_color(anc_depth))
                        self.tree_list.addItem(anc_item)
                        self.path_map[anc_line] = anc_path

                if full_path not in seen_paths:
                    seen_paths.add(full_path)
                    match_item = QListWidgetItem(display_line)
                    match_item.setData(Qt.ItemDataRole.ForegroundRole, self._get_depth_color(depth))
                    self.tree_list.addItem(match_item)
                    self.path_map[display_line] = full_path

        self.tree_list.setUpdatesEnabled(True)

    def _on_search_return(self):
        if self.tree_list.count() == 1:
            item = self.tree_list.item(0)
            self.on_item_double_clicked(item)

    def update_tree(self):
        if not self.target_path:
            return

        self._cancel_worker()
        self.tree_list.clear()
        self.path_map = {}
        self._all_tree_items = []

        root_name = f"📂 {os.path.basename(self.target_path)} [сканирование...]"
        self.tree_list.setUpdatesEnabled(False)
        root_item = QListWidgetItem(root_name)
        root_item.setData(Qt.ItemDataRole.ForegroundRole, self._get_depth_color(0))
        self.tree_list.addItem(root_item)
        self.path_map[root_name] = self.target_path
        self.tree_list.setUpdatesEnabled(True)

        self.radar_anim.stop()
        self.radar_anim.start()

        self.worker = ScanWorker(self.target_path, self.max_depth, self.count_files)
        self.worker.batch_ready.connect(self._on_batch_ready, Qt.ConnectionType.QueuedConnection)
        self.worker.result_ready.connect(self._on_scan_finished, Qt.ConnectionType.QueuedConnection)
        self.worker.start()

    def _on_batch_ready(self, batch, total_folders, is_final):
        try:
            self._all_tree_items.extend(batch)
            self.tree_list.setUpdatesEnabled(False)
            for display_line, full_path, depth in batch:
                item = QListWidgetItem(display_line)
                item.setData(Qt.ItemDataRole.ForegroundRole, self._get_depth_color(depth))
                self.tree_list.addItem(item)
                self.path_map[display_line] = full_path

            root_item = self.tree_list.item(0)
            if root_item:
                new_root = f"📂 {os.path.basename(self.target_path)} [папок: {total_folders}...]"
                root_item.setText(new_root)
                root_item.setData(Qt.ItemDataRole.ForegroundRole, self._get_depth_color(0))
                self.path_map[new_root] = self.target_path

            self.tree_list.setUpdatesEnabled(True)
        except Exception:
            log.error("_on_batch_ready CRASH", exc_info=True)

    def _on_scan_finished(self, tree_data, total_folders):
        try:
            root_item = self.tree_list.item(0)
            if root_item:
                count_files = self.count_files
                root_files_count = 0
                if count_files:
                    try:
                        with os.scandir(self.target_path) as entries:
                            root_files_count = sum(1 for e in entries if e.is_file(follow_symlinks=False))
                    except Exception:
                        pass
                files_text = f" [{root_files_count} f]" if count_files else ""
                root_name = f"📂 {os.path.basename(self.target_path)}{files_text} [Всего папок: {total_folders}]"
                root_item.setText(root_name)
                root_item.setData(Qt.ItemDataRole.ForegroundRole, self._get_depth_color(0))
                self.path_map[root_name] = self.target_path
            self.update()
        except Exception:
            log.error("_on_scan_finished CRASH", exc_info=True)

    def on_item_double_clicked(self, item):
        if item.text() in self.path_map:
            target_path = self.path_map[item.text()]
            if os.path.exists(target_path):
                os.startfile(target_path)
                self.hide_overlay()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.hide_overlay()
        super().keyPressEvent(event)

    def closeEvent(self, event):
        self._cancel_worker()
        QApplication.quit()
        super().closeEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_position = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
        elif event.button() == Qt.MouseButton.RightButton:
            self.search_enabled = not self.search_enabled
            self.search_input.setVisible(self.search_enabled)
            self.save_settings()
            if self.search_enabled:
                self.search_input.setFocus()
            else:
                self.search_input.clear()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self.drag_position)
            event.accept()


def pipe_listener(bridge_obj):
    pipe_name = r'\\.\pipe\overtree_pipe'
    while True:
        h_pipe = win32pipe.CreateNamedPipe(
            pipe_name, win32pipe.PIPE_ACCESS_INBOUND,
            win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT,
            1, 65536, 65536, 0, None
        )
        try:
            win32pipe.ConnectNamedPipe(h_pipe, None)
            err, data = win32file.ReadFile(h_pipe, 4096)
            path = data.decode('utf-8', errors='ignore').strip()
            if path:
                bridge_obj.path_received.emit(path)
        except Exception:
            log.error("pipe_listener error", exc_info=True)
        finally:
            try:
                win32file.CloseHandle(h_pipe)
            except Exception:
                pass


if __name__ == "__main__":
    app = QApplication(sys.argv)
    comm_bridge = PipeCommBridge()
    overlay = FolderTreeOverlay(comm_bridge)

    thread = threading.Thread(target=pipe_listener, args=(comm_bridge,), daemon=True)
    thread.start()
    sys.exit(app.exec())
