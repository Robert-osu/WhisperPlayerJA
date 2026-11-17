import bisect
import json
import math
import os
import subprocess
import sys
import hashlib
from dataclasses import dataclass

from fugashi import Tagger
from japanese_dictionary import JapaneseDictionary

from PySide6.QtCore import (
    Qt, QUrl, QTimer, QThread, Signal, QRectF,
    QPropertyAnimation, QEasingCurve, QEvent,
    QObject, Slot, QSettings, Property,
    QPointF
)
from PySide6.QtGui import (
    QColor, QFont, QKeySequence, QPainter, QPen, QBrush,
    QTextCursor, QTextCharFormat, QShortcut,
    QPixmap, QPolygonF
)
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QSlider, QTextEdit, QLabel, QFileDialog, QFrame, QMessageBox,
    QComboBox, QSizePolicy, QScrollArea,
    QGraphicsOpacityEffect
)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput


APP_QSS = """
QWidget {
    background: #1e1f22;
    color: #e6e6e6;
    font-family: "Inter", "Segoe UI", "Noto Sans", sans-serif;
    font-size: 14px;
}
QLabel#Title { font-size: 19px; font-weight: 700; color: #f5f5f5; }
QLabel#Muted { color: #969ba5; font-size: 12px; }
QLabel#Time { color: #aeb3bd; font-family: "JetBrains Mono", monospace; font-size: 12px; }
QPushButton {
    background: #2b2d31; border: 1px solid #3a3d42;
    border-radius: 8px; padding: 8px 13px;
}
QPushButton:hover { background: #35383e; border-color: #555b66; }
QPushButton:pressed { background: #232428; }
QPushButton#Primary {
    background: #5865f2; border: none; font-weight: 600;
    min-width: 112px; padding: 9px 16px;
}
QPushButton#Primary:hover { background: #6b76f5; }
QPushButton#Danger { color: #ff8585; border-color: #654040; }
QFrame#Panel { background: #25262b; border: 1px solid #303238; border-radius: 12px; }
QTextEdit {
    background: #151618; border: 1px solid #303238; border-radius: 10px;
    padding: 18px; font-size: 17px; selection-background-color: #5865f2;
}
QSlider::groove:horizontal { height: 6px; background: #373940; border-radius: 3px; }
QSlider::sub-page:horizontal { background: #5865f2; border-radius: 3px; }
QSlider::handle:horizontal {
    background: #f4f5ff; width: 14px; height: 14px;
    margin: -4px 0; border-radius: 7px;
}
QComboBox {
    background: #2b2d31; border: 1px solid #3a3d42;
    border-radius: 7px; padding: 6px 10px; min-width: 76px;
}
"""


def fmt_time(ms: int) -> str:
    seconds = max(0, ms // 1000)
    return f"{seconds // 3600:02d}:{(seconds // 60) % 60:02d}:{seconds % 60:02d}"


def rebuild_japanese_word_segments(segments, tagger):
    """
    Перестраивает японские слова по морфологическим границам Fugashi.
    Временные метки берутся из исходных WhisperX words.
    """
    result = []

    for segment in segments:
        text = str(segment.get("text", ""))
        source_words = segment.get("words", [])

        if not text or not source_words:
            result.extend(source_words)
            continue

        # Получаем морфологические токены.
        tokens = [
            node.surface
            for node in tagger(text)
            if node.surface
        ]

        # Разворачиваем исходные word_segments в символы
        # с сохранением временных меток.
        timed_chars = []

        for item in source_words:
            token = str(item.get("word", ""))

            try:
                start = float(item["start"])
                end = float(item["end"])
            except (KeyError, TypeError, ValueError):
                continue

            if not math.isfinite(start) or not math.isfinite(end):
                continue

            for char in token:
                if char.isspace():
                    continue

                timed_chars.append({
                    "char": char,
                    "start": start,
                    "end": end,
                    "score": float(item.get("score", 0.0) or 0.0)
                })

        # Убираем пробелы для сопоставления текста.
        source_text = "".join(
            char for char in text if not char.isspace()
        )

        timed_text = "".join(
            item["char"] for item in timed_chars
        )

        token_text = "".join(
            char
            for token in tokens
            for char in token
            if not char.isspace()
        )

        # При несовпадении не меняем исходные интервалы.
        if source_text != timed_text or source_text != token_text:
            result.extend(source_words)
            continue

        position = 0

        for token in tokens:
            token_chars = [
                char for char in token if not char.isspace()
            ]

            if not token_chars:
                continue

            count = len(token_chars)
            matched = timed_chars[position:position + count]

            if len(matched) != count:
                break

            result.append({
                "word": token,
                "start": matched[0]["start"],
                "end": matched[-1]["end"],
                "score": (
                    sum(item["score"] for item in matched) / count
                )
            })

            position += count

    return result


class TranslationManager:
    def __init__(self):
        self.cache = {}

    @staticmethod
    def make_cache_key(segment):
        raw = (
            f"{segment.start:.3f}|"
            f"{segment.end:.3f}|"
            f"{segment.text}"
        )
        return hashlib.sha256(
            raw.encode("utf-8")
        ).hexdigest()

    def prepare(self, segments, cache_path):
        self.cache = {}

        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.cache = data.get("translations", {})
            except (OSError, json.JSONDecodeError, TypeError):
                self.cache = {}

        ready = sum(
            bool(self.cache.get(s.cache_key))
            for s in segments
        )

        return self.cache.copy(), len(segments) - ready

    @staticmethod
    def save_cache(cache, cache_path):
        directory = os.path.dirname(cache_path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        temp_path = cache_path + ".tmp"

        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(
                {"translations": cache},
                f,
                ensure_ascii=False,
                indent=2
            )

        os.replace(temp_path, cache_path)


class TranslationWorker(QObject):
    prepareRequested = Signal(list, str)
    cacheLoaded = Signal(dict, int)
    preparationFinished = Signal(str)

    def __init__(self):
        super().__init__()

        self.manager = TranslationManager()

        self.prepareRequested.connect(
            self.prepare,
            Qt.ConnectionType.QueuedConnection
        )

    @Slot(list, str)
    def prepare(self, segments, cache_path):
        try:
            cache, missing = self.manager.prepare(
                segments,
                cache_path
            )

            self.cacheLoaded.emit(cache, missing)

            if missing:
                message = (
                    f"Кэш загружен. Не хватает переводов: {missing}. "
                    "Переводчик пока не подключён."
                )
            else:
                message = "Все переводы загружены из кэша."

            self.preparationFinished.emit(message)

        except Exception as exc:
            self.preparationFinished.emit(
                f"Ошибка загрузки переводов: {exc}"
            )

@dataclass
class WordSpan:
    start: float
    end: float

    # Индексация Python
    char_start: int
    char_len: int

    # Индексация Qt (UTF-16)
    qt_char_start: int
    qt_char_len: int

def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2

class WaveformWorker(QThread):
    ready = Signal(list)
    failed = Signal(str)

    def __init__(self, path: str, width: int = 2400):
        super().__init__()
        self.path = path
        self.width = width

    def run(self):
        # FFmpeg decodes any supported input to low-rate mono float PCM.
        command = [
            "ffmpeg", "-v", "error", "-i", self.path,
            "-vn", "-ac", "1", "-ar", "8000", "-f", "f32le", "pipe:1"
        ]
        try:
            proc = subprocess.run(command, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, check=True)
            import array
            samples = array.array("f")
            samples.frombytes(proc.stdout)
            if sys.byteorder != "little":
                samples.byteswap()
            if not samples:
                raise RuntimeError("FFmpeg не вернул аудиоданные.")
            block = max(1, math.ceil(len(samples) / self.width))
            peaks = []
            for i in range(0, len(samples), block):
                chunk = samples[i:i + block]
                peak = max((abs(x) for x in chunk), default=0.0)
                peaks.append(min(1.0, peak))
            self.ready.emit(peaks)
        except FileNotFoundError:
            self.failed.emit("Не найден ffmpeg. Установи его: sudo pacman -S ffmpeg")
        except subprocess.CalledProcessError as exc:
            message = exc.stderr.decode("utf-8", "replace")[-1200:]
            self.failed.emit("Не удалось построить waveform:\n" + message)
        except Exception as exc:
            self.failed.emit(str(exc))


class WaveformWidget(QWidget):
    seekRequested = Signal(int)

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(112)
        self.setMaximumHeight(140)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.peaks = []
        self.position_ms = 0
        self.duration_ms = 0
        self.setToolTip("Нажми на волну, чтобы перейти к этому месту")

    def set_peaks(self, peaks):
        self.peaks = peaks
        self.update()

    def set_playhead(self, position, duration):
        self.position_ms = position
        self.duration_ms = duration
        self.update()

    def mousePressEvent(self, event):
        if self.duration_ms <= 0 or not self.peaks:
            return
        x = min(max(0, event.position().x()), self.width())
        fraction = x / max(1, self.width())
        self.seekRequested.emit(int(fraction * self.duration_ms))

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(2, 5, -2, -5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#17181b"))
        p.drawRoundedRect(rect, 8, 8)

        if not self.peaks:
            p.setPen(QColor("#858b96"))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                       "Открой аудио — здесь появится звуковая волна")
            return

        center = rect.center().y()
        half_h = rect.height() * 0.42
        bar_step = rect.width() / len(self.peaks)
        play_x = rect.left()
        if self.duration_ms > 0:
            play_x += rect.width() * self.position_ms / self.duration_ms

        for i, peak in enumerate(self.peaks):
            x = rect.left() + i * bar_step
            h = max(2.0, peak * half_h)
            color = QColor("#8790a5") if x > play_x else QColor("#7885ff")
            p.setBrush(color)
            p.drawRoundedRect(QRectF(x, center - h, max(1.2, bar_step * 0.66), h * 2), 1.5, 1.5)

        p.setPen(QPen(QColor("#ffffff"), 1.5))
        p.drawLine(int(play_x), int(rect.top()), int(play_x), int(rect.bottom()))


class WordInfoPanel(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.setObjectName("WordInfoPanel")
        self.setFixedWidth(360)
        self.setMinimumHeight(480)

        self.setStyleSheet("""
            QFrame#WordInfoPanel {
                background: #25262d;
                border: 1px solid #414451;
                border-radius: 14px;
            }

            QLabel {
                background: transparent;
                border: none;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(18)

        # Заголовок панели
        header = QLabel("WORD INFORMATION")
        header.setMinimumHeight(22)
        header.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed
        )
        header.setStyleSheet("""
            color: #858bff;
            font-size: 11px;
            font-weight: bold;
            letter-spacing: 2px;
        """)
        layout.addWidget(header)

        # Основное слово
        self.word_label = QLabel("—")
        self.word_label.setAlignment(
            Qt.AlignmentFlag.AlignCenter
        )
        self.word_label.setWordWrap(True)
        self.word_label.setMinimumHeight(100)
        self.word_label.setStyleSheet("""
            color: #ffffff;
            font-size: 52px;
            font-weight: bold;
            padding: 8px;
        """)
        layout.addWidget(self.word_label)

        # Чтение
        self.phonetic_label = QLabel("[ — ]")
        self.phonetic_label.setAlignment(
            Qt.AlignmentFlag.AlignCenter
        )
        self.phonetic_label.setMinimumHeight(28)
        self.phonetic_label.setStyleSheet("""
            color: #a0a5b5;
            font-size: 17px;
        """)
        layout.addWidget(self.phonetic_label)

        self.add_separator(layout)

        # Информационные блоки
        self.translation_label = self.add_section(
            layout, "ПЕРЕВОД", "Перевод не найден"
        )

        self.pronunciation_label = self.add_section(
            layout, "РОМАНИЗАЦИЯ", "—"
        )

        self.part_label = self.add_section(
            layout, "ЧАСТЬ РЕЧИ", "—"
        )

        self.example_label = self.add_section(
            layout, "ПРИМЕР", "—"
        )

        layout.addStretch(1)

        self.add_separator(layout)

        hint = QLabel("Удерживай TAB для просмотра")
        hint.setAlignment(
            Qt.AlignmentFlag.AlignCenter
        )
        hint.setMinimumHeight(22)
        hint.setStyleSheet("""
            color: #777d8b;
            font-size: 11px;
        """)
        layout.addWidget(hint)

    def add_separator(self, layout):
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet("""
            background: #414451;
            border: none;
        """)
        layout.addWidget(line)

    def add_section(self, layout, title, value):
        section = QWidget()
        section.setStyleSheet("""
            QWidget {
                background: transparent;
                border: none;
            }
        """)

        section_layout = QVBoxLayout(section)
        section_layout.setContentsMargins(0, 0, 0, 0)
        section_layout.setSpacing(7)

        heading = QLabel(title)
        heading.setMinimumHeight(20)
        heading.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed
        )
        heading.setStyleSheet("""
            color: #858bff;
            font-size: 10px;
            font-weight: bold;
            letter-spacing: 1px;
        """)

        value_label = QLabel(value)
        value_label.setWordWrap(True)
        value_label.setMinimumHeight(24)
        value_label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Minimum
        )
        value_label.setStyleSheet("""
            color: #e0e2e9;
            font-size: 14px;
            padding-bottom: 3px;
        """)

        section_layout.addWidget(heading)
        section_layout.addWidget(value_label)

        layout.addWidget(section)

        return value_label

    def set_word(self, word, info=None):
        info = info or {}

        self.word_label.setText(word)

        self.phonetic_label.setText(
            info.get("phonetic", "[ — ]")
        )

        self.translation_label.setText(
            info.get("translation", "Перевод не найден")
        )

        self.pronunciation_label.setText(
            info.get("pronunciation", "—")
        )

        self.part_label.setText(
            info.get("part_of_speech", "—")
        )

        self.example_label.setText(
            info.get("example", "—")
        )

@dataclass
class SegmentSpan:
    start: float
    end: float
    text: str
    cache_key: str


class SegmentTranslationPanel(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.setObjectName("SegmentTranslationPanel")
        self.setFixedWidth(360)

        self.setStyleSheet("""
            QFrame#SegmentTranslationPanel {
                background: #25262d;
                border: 1px solid #414451;
                border-radius: 14px;
            }
            QLabel {
                background: transparent;
                border: none;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(14)

        header = QLabel("SEGMENT TRANSLATION")
        header.setStyleSheet("""
            color: #858bff;
            font-size: 11px;
            font-weight: bold;
            letter-spacing: 1px;
        """)
        layout.addWidget(header)

        self.segment_label = QLabel("Сегмент не выбран")
        self.segment_label.setWordWrap(True)
        self.segment_label.setStyleSheet("""
            color: #a0a5b5;
            font-size: 14px;
        """)
        layout.addWidget(self.segment_label)

        separator = QFrame()
        separator.setFixedHeight(1)
        separator.setStyleSheet("background: #414451;")
        layout.addWidget(separator)

        translation_title = QLabel("ПЕРЕВОД")
        translation_title.setStyleSheet("""
            color: #858bff;
            font-size: 10px;
            font-weight: bold;
            letter-spacing: 1px;
        """)
        layout.addWidget(translation_title)

        self.translation_label = QLabel(
            "Перевод ещё не подготовлен"
        )
        self.translation_label.setWordWrap(True)
        self.translation_label.setAlignment(
            Qt.AlignmentFlag.AlignTop
        )
        self.translation_label.setStyleSheet("""
            color: #e0e2e9;
            font-size: 16px;
            padding-top: 4px;
        """)
        layout.addWidget(self.translation_label, 1)

        self.status_label = QLabel("Ожидание перевода")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("""
            color: #858b96;
            font-size: 11px;
        """)
        layout.addWidget(self.status_label)

        hint = QLabel("Сочетание Ctrl + Tab фиксирует панели.")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet("""
            color: #777d8b;
            font-size: 11px;
        """)
        layout.addWidget(hint)

    def set_segment(self, segment, translation=None, status=None):
        if segment is None:
            self.segment_label.setText("Сегмент не выбран")
            self.translation_label.setText("—")
            self.status_label.setText("")
            return

        self.segment_label.setText(segment.text)

        if translation:
            self.translation_label.setText(translation)
            self.status_label.setText("Перевод готов")
        else:
            self.translation_label.setText(
                "Перевод ещё не подготовлен"
            )
            self.status_label.setText(
                status or "Ожидает подключения переводчика"
            )

class DictionaryWorker(QObject):
    ready = Signal(bool, str)
    resultReady = Signal(int, int, str, dict)

    def __init__(self):
        super().__init__()
        self.dictionary = None

    @Slot()
    def initialize(self):
        try:
            self.dictionary = JapaneseDictionary()
            self.ready.emit(True, "Японский словарь готов")
        except Exception as exc:
            self.ready.emit(False, str(exc))

    @Slot(int, int, str)
    def lookup(self, generation, index, token):
        if not self.dictionary:
            return

        try:
            info = self.dictionary.lookup(token)

        except Exception as exc:
            info = {
                "translation": f"Ошибка поиска: {exc}",
                "phonetic": "[ — ]",
                "pronunciation": "—",
                "part_of_speech": "—",
                "example": "—"
            }

        self.resultReady.emit(
            generation,
            index,
            token,
            info
        )


class AnimatedPlayButton(QPushButton):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.setFixedSize(60, 60)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._hover = 0.0
        self._press = 0.0
        self._playing = False

        # Анимация свечения
        self.hover_animation = QPropertyAnimation(
            self, b"hoverProgress", self
        )
        self.hover_animation.setDuration(220)
        self.hover_animation.setEasingCurve(
            QEasingCurve.Type.OutCubic
        )

        # Анимация нажатия
        self.press_animation = QPropertyAnimation(
            self, b"pressProgress", self
        )
        self.press_animation.setDuration(110)
        self.press_animation.setEasingCurve(
            QEasingCurve.Type.OutCubic
        )

        self.pressed.connect(self.animate_press)
        self.released.connect(self.animate_release)

        self.setToolTip("Воспроизвести / Пауза")

    @Property(float)
    def hoverProgress(self):
        return self._hover

    @hoverProgress.setter
    def hoverProgress(self, value):
        self._hover = value
        self.update()

    @Property(float)
    def pressProgress(self):
        return self._press

    @pressProgress.setter
    def pressProgress(self, value):
        self._press = value
        self.update()

    def animate_hover(self, value):
        self.hover_animation.stop()
        self.hover_animation.setStartValue(self._hover)
        self.hover_animation.setEndValue(value)
        self.hover_animation.start()

    def animate_press(self):
        self.press_animation.stop()
        self.press_animation.setStartValue(self._press)
        self.press_animation.setEndValue(1.0)
        self.press_animation.start()

    def animate_release(self):
        self.press_animation.stop()
        self.press_animation.setStartValue(self._press)
        self.press_animation.setEndValue(0.0)
        self.press_animation.start()

    def enterEvent(self, event):
        self.animate_hover(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.animate_hover(0.0)
        super().leaveEvent(event)

    def set_playing(self, playing):
        self._playing = playing
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        size = min(self.width(), self.height())
        center = self.rect().center()

        # Масштаб при нажатии
        scale = 1.0 - self._press * 0.08

        p.translate(center)
        p.scale(scale, scale)
        p.translate(-center)

        # Цвета
        base = QColor("#5865f2")
        hover = QColor("#8992ff")

        # Свечение
        glow_alpha = int(25 + self._hover * 75)

        glow = QColor(104, 116, 255, glow_alpha)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(glow)

        glow_radius = 17 + self._hover * 4

        p.drawEllipse(
            QRectF(
                center.x() - glow_radius,
                center.y() - glow_radius,
                glow_radius * 2,
                glow_radius * 2
            )
        )

        # Фон кнопки
        background = QColor("#2b2d38")
        background = background.lighter(
            int(100 + self._hover * 12)
        )

        p.setBrush(background)
        p.drawEllipse(
            QRectF(
                center.x() - 20,
                center.y() - 20,
                40,
                40
            )
        )

        # Внешнее кольцо
        ring_color = QColor(
            88 + int(self._hover * 35),
            101 + int(self._hover * 25),
            242
        )

        ring_color.setAlpha(
            150 + int(self._hover * 105)
        )

        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(ring_color, 1.5))
        p.drawEllipse(
            QRectF(
                center.x() - 19,
                center.y() - 19,
                38,
                38
            )
        )

        # Иконка
        icon_color = QColor("#f5f6ff")
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(icon_color)

        if self._playing:
            # Pause
            bar_width = 5.5
            bar_height = 17

            p.drawRoundedRect(
                QRectF(
                    center.x() - 9,
                    center.y() - bar_height / 2,
                    bar_width,
                    bar_height
                ),
                2, 2
            )

            p.drawRoundedRect(
                QRectF(
                    center.x() + 3.5,
                    center.y() - bar_height / 2,
                    bar_width,
                    bar_height
                ),
                2, 2
            )

        else:
            # Play
            triangle = QPolygonF([
                QPointF(center.x() - 5, center.y() - 8),
                QPointF(center.x() + 8, center.y()),
                QPointF(center.x() - 5, center.y() + 8)
            ])

            p.drawPolygon(triangle)

        p.end()


class AnimatedSeekButton(QPushButton):
    def __init__(self, direction="next", parent=None):
        super().__init__(parent)

        self.direction = direction
        self.setFixedSize(40, 40)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip(
            "Следующее слово" if direction == "next"
            else "Предыдущее слово"
        )

        self._hover = 0.0
        self._press = 0.0

        self.hover_animation = QPropertyAnimation(
            self, b"hoverProgress", self
        )
        self.hover_animation.setDuration(180)
        self.hover_animation.setEasingCurve(
            QEasingCurve.Type.OutCubic
        )

        self.press_animation = QPropertyAnimation(
            self, b"pressProgress", self
        )
        self.press_animation.setDuration(100)
        self.press_animation.setEasingCurve(
            QEasingCurve.Type.OutCubic
        )

        self.pressed.connect(self.animate_press)
        self.released.connect(self.animate_release)

    @Property(float)
    def hoverProgress(self):
        return self._hover

    @hoverProgress.setter
    def hoverProgress(self, value):
        self._hover = value
        self.update()

    @Property(float)
    def pressProgress(self):
        return self._press

    @pressProgress.setter
    def pressProgress(self, value):
        self._press = value
        self.update()

    def animate_hover(self, value):
        self.hover_animation.stop()
        self.hover_animation.setStartValue(self._hover)
        self.hover_animation.setEndValue(value)
        self.hover_animation.start()

    def animate_press(self):
        self.press_animation.stop()
        self.press_animation.setStartValue(self._press)
        self.press_animation.setEndValue(1.0)
        self.press_animation.start()

    def animate_release(self):
        self.press_animation.stop()
        self.press_animation.setStartValue(self._press)
        self.press_animation.setEndValue(0.0)
        self.press_animation.start()

    def enterEvent(self, event):
        self.animate_hover(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.animate_hover(0.0)
        super().leaveEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        center = self.rect().center()

        scale = 1.0 - self._press * 0.08
        p.translate(center)
        p.scale(scale, scale)
        p.translate(-center)

        # Фон
        bg = QColor("#2b2d38")
        bg = bg.lighter(int(100 + self._hover * 15))

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bg)
        p.drawEllipse(
            QRectF(center.x() - 17, center.y() - 17, 34, 34)
        )

        # Контур
        ring = QColor(
            88 + int(self._hover * 35),
            101 + int(self._hover * 25),
            242
        )
        ring.setAlpha(120 + int(self._hover * 135))

        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(ring, 1.4))
        p.drawEllipse(
            QRectF(center.x() - 17, center.y() - 17, 34, 34)
        )

        # Иконка
        icon_color = QColor("#f5f6ff")
        p.setPen(QPen(icon_color, 2.2,
                      Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap,
                      Qt.PenJoinStyle.RoundJoin))

        cx = center.x()
        cy = center.y()

        p.setBrush(Qt.BrushStyle.NoBrush)

        if self.direction == "next":
            # Вертикальная черта
            p.drawLine(
                QPointF(cx - 7, cy - 7),
                QPointF(cx - 7, cy + 7)
            )

            # Стрелка вправо
            p.drawLine(
                QPointF(cx - 3, cy),
                QPointF(cx + 7, cy)
            )
            p.drawLine(
                QPointF(cx + 2, cy - 5),
                QPointF(cx + 7, cy)
            )
            p.drawLine(
                QPointF(cx + 2, cy + 5),
                QPointF(cx + 7, cy)
            )

        else:
            # Вертикальная черта
            p.drawLine(
                QPointF(cx + 7, cy - 7),
                QPointF(cx + 7, cy + 7)
            )

            # Стрелка влево
            p.drawLine(
                QPointF(cx + 3, cy),
                QPointF(cx - 7, cy)
            )
            p.drawLine(
                QPointF(cx - 2, cy - 5),
                QPointF(cx - 7, cy)
            )
            p.drawLine(
                QPointF(cx - 2, cy + 5),
                QPointF(cx - 7, cy)
            )

        p.end()


class WhisperPlayer(QWidget):
    dictionaryLookupRequested = Signal(int, int, str)

    def __init__(self):
        super().__init__()

        self.document_generation = 0

        self.setWindowTitle("WhisperX Player")
        self.resize(1080, 760)
        self.setMinimumSize(780, 560)

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)

        # Настройки и восстановление сессии
        self.settings = QSettings(
            "WhisperXPlayer",
            "WhisperXPlayer"
        )

        self.json_path = ""
        self._restore_position_ms = 0

        # Режим закрепления подсказки Tab
        self.tab_hint_pinned = False
        self.tab_is_pressed = False
        self.ctrl_tab_handled = False

        # Закрепление боковых панелей через Ctrl + Tab
        self.panels_pinned = False

        self.player.setAudioOutput(self.audio)
        self.audio.setVolume(0.85)

        self.words: list[WordSpan] = []
        self.word_starts = []
        self.duration_ms = 0
        self.media_path = ""
        self.wave_worker = None
        self.current_index = -1

        self.word_info = {}

        self.segments = []
        self.word_segment_indices = []
        self.segment_translations = {}
        self.translation_cache_path = ""
        self.current_segment_index = -1

        self._build_ui()
        self.create_tab_hint()
        self.create_ctrl_hint()
        self.set_hints_visible(True)
        # Морфологический анализатор японского текста.
        self.ja_tagger = Tagger()

        # Поток японского словаря
        self.dictionary_thread = QThread(self)
        self.dictionary_worker = DictionaryWorker()

        self.dictionary_worker.moveToThread(
            self.dictionary_thread
        )

        self.dictionary_thread.started.connect(
            self.dictionary_worker.initialize
        )

        self.dictionaryLookupRequested.connect(
            self.dictionary_worker.lookup,
            Qt.ConnectionType.QueuedConnection
        )

        self.dictionary_worker.ready.connect(
            self.on_dictionary_ready
        )

        self.dictionary_worker.resultReady.connect(
            self.on_dictionary_result
        )

        # Поток подготовки переводов
        self.translation_thread = QThread(self)
        self.translation_worker = TranslationWorker()

        self.translation_worker.moveToThread(
            self.translation_thread
        )

        self.translation_worker.cacheLoaded.connect(
            self.on_translation_cache_loaded
        )

        self.translation_worker.preparationFinished.connect(
            self.on_translation_preparation_finished
        )

        self._connect_signals()
        self._shortcuts()

        QApplication.instance().installEventFilter(self)

        # Запускаем потоки после полной настройки
        self.dictionary_thread.start()
        self.translation_thread.start()

        # Восстанавливаем предыдущую сессию после создания UI.
        QTimer.singleShot(0, self.restore_session)

    def update_content_margins(self, expanded):
        pass

    
    def create_tab_hint(self):
        self.tab_hint = QLabel(self)
        self.tab_hint.setObjectName("TabHint")
        self.tab_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tab_hint.setFixedSize(64, 42)

        self.tab_hint.setText(
            '<span style="'
            'background-color: #f1f1f4;'
            'border: 1px solid #dedee5;'
            'border-radius: 5px;'
            'padding: 4px 9px;'
            'color: #444450;'
            'font-size: 13px;'
            'font-weight: 700;'
            '">Tab</span>'
            '<span style="'
            'color: #777783;'
            'font-size: 10px;'
            'margin-left: 7px;'
            '">Жми</span>'
        )

        self.tab_hint.setStyleSheet("""
            QLabel#TabHint {
                background-color: rgba(255, 255, 255, 245);
                border: 1px solid #e6e6ec;
                border-radius: 12px;
                padding: 4px;
            }
        """)

        self.tab_hint_effect = QGraphicsOpacityEffect(self.tab_hint)
        self.tab_hint.setGraphicsEffect(self.tab_hint_effect)
        self.tab_hint_effect.setOpacity(0.0)

        self.tab_hint_animation = QPropertyAnimation(
            self.tab_hint_effect, b"opacity", self
        )
        self.tab_hint_animation.setDuration(350)
        self.tab_hint_animation.setEasingCurve(
            QEasingCurve.Type.OutCubic
        )

        self.tab_hint.hide()

    def create_ctrl_hint(self):
        self.ctrl_tab_hint = QLabel(self)
        self.ctrl_tab_hint.setObjectName("CtrlTabHint")
        self.ctrl_tab_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.ctrl_tab_hint.setFixedSize(64, 42)

        self.ctrl_tab_hint.setText(
            '<span style="'
            'background-color: #f1f1f4;'
            'border: 1px solid #dedee5;'
            'border-radius: 5px;'
            'padding: 4px 9px;'
            'color: #444450;'
            'font-size: 13px;'
            'font-weight: 700;'
            '">Ctrl+Tab</span>'
            '<span style="'
            'color: #777783;'
            'font-size: 10px;'
            'margin-left: 7px;'
            '">Жми</span>'
        )

        self.ctrl_tab_hint.setStyleSheet("""
            QLabel#CtrlTabHint {
                background-color: rgba(255, 255, 255, 245);
                border: 1px solid #e6e6ec;
                border-radius: 12px;
                padding: 4px;
            }
        """)

        self.ctrl_tab_hint_effect = QGraphicsOpacityEffect(self.ctrl_tab_hint)
        self.ctrl_tab_hint.setGraphicsEffect(self.ctrl_tab_hint_effect)
        self.ctrl_tab_hint_effect.setOpacity(0.0)

        self.ctrl_tab_hint_animation = QPropertyAnimation(
            self.ctrl_tab_hint_effect, b"opacity", self
        )
        self.ctrl_tab_hint_animation.setDuration(350)
        self.ctrl_tab_hint_animation.setEasingCurve(
            QEasingCurve.Type.OutCubic
        )

        self.ctrl_tab_hint.hide()
        
    def set_hints_visible(self, visible):
        hints = [
            (self.tab_hint, self.tab_hint_animation,
            self.tab_hint_effect),
            (self.ctrl_tab_hint, self.ctrl_tab_hint_animation,
            self.ctrl_tab_hint_effect),
        ]

        for hint, animation, effect in hints:
            animation.stop()

            if visible:
                hint.show()
                hint.raise_()

            animation.setStartValue(effect.opacity())
            animation.setEndValue(1.0 if visible else 0.0)
            animation.start()


    def update_tab_hint_position(self):
        if not hasattr(self, "tab_hint"):
            return

        if not hasattr(self, "ctrl_tab_hint"):
            return

        margin = 12
        spacing = 8

        total_height = (
            self.tab_hint.height()
            + self.ctrl_tab_hint.height()
            + spacing
        )

        start_y = (self.height() - total_height) // 2
        x = self.width() - max(
            self.tab_hint.width(),
            self.ctrl_tab_hint.width()
        ) - margin

        self.tab_hint.move(x, start_y)

        self.ctrl_tab_hint.move(
            x,
            start_y + self.tab_hint.height() + spacing
        )

    def save_session(self):
        """Сохраняет текущую сессию."""

        self.settings.setValue(
            "media_path",
            self.media_path
        )

        self.settings.setValue(
            "json_path",
            self.json_path
        )

        self.settings.setValue(
            "position_ms",
            self.player.position()
        )

        self.settings.sync()


    def restore_session(self):
        """Восстанавливает последнюю сессию."""

        media_path = self.settings.value(
            "media_path", "", type=str
        )

        json_path = self.settings.value(
            "json_path", "", type=str
        )

        position = self.settings.value(
            "position_ms", 0, type=int
        )

        restored = []

        # Сначала загружаем медиа.
        if media_path and os.path.isfile(media_path):
            self.open_media(
                path=media_path,
                restore_position=position
            )
            restored.append("аудио")

        # Затем JSON с распознанной речью.
        if json_path and os.path.isfile(json_path):
            self.open_json(path=json_path)
            restored.append("JSON")

        if restored:
            self.status.setText(
                "Восстановлена сессия: " + " + ".join(restored)
            )
        else:
            self.status.setText(
                "Открой аудио и соответствующий JSON WhisperX"
            )
    
    def on_translation_cache_loaded(self, cache, missing):
        self.segment_translations = cache

        # Обновляем панель, если сегмент уже выбран.
        self.update_segment_panel()

    def on_translation_preparation_finished(self, message):
        self.status.setText(message)

    def update_segment_panel(self):
        if not hasattr(self, "segment_panel"):
            return

        idx = self.current_segment_index

        if not (0 <= idx < len(self.segments)):
            self.segment_panel.set_segment(None)
            return

        segment = self.segments[idx]
        translation = self.segment_translations.get(
            segment.cache_key
        )

        self.segment_panel.set_segment(
            segment,
            translation
        )

    def on_dictionary_ready(self, success, message):
        self.status.setText(message)

        if not success:
            print("Dictionary initialization failed:", message)

    def on_panel_animation_finished(self):
        if not self.panel_visible:
            if (
                self.panel_animation.state()
                == QPropertyAnimation.State.Stopped
                and
                self.segment_panel_animation.state()
                == QPropertyAnimation.State.Stopped
            ):
                self.word_panel.hide()
                self.segment_panel.hide()

                self.update_content_margins(False)

    def on_dictionary_result(
        self,
        generation,
        index,
        token,
        info
    ):
        if generation != self.document_generation:
            return

        if index != self.current_index:
            return

        self.word_panel.set_word(token, info)


    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_tab_hint_position()

        if not hasattr(self, "word_panel"):
            return

        if self.panel_visible:
            self.panel_animation.stop()
            self.segment_panel_animation.stop()

            self.update_content_margins(True)

            height = max(300, self.height() - 110)
            y = 90

            right_width = self.word_panel.width()
            left_width = self.segment_panel.width()

            self.word_panel.setGeometry(
                self.width() - right_width - self.panel_margin,
                y,
                right_width,
                height
            )

            self.segment_panel.setGeometry(
                self.panel_margin,
                y,
                left_width,
                height
            )

            self.word_panel.show()
            self.segment_panel.show()

            self.word_panel.raise_()
            self.segment_panel.raise_()
        else:
            self.word_panel.hide()
            self.segment_panel.hide()


    def toggle_word_panel(self, visible):
        if self.panel_visible == visible:
            return

        self.panel_visible = visible


        self.set_hints_visible(not visible)

        self.panel_animation.stop()
        self.segment_panel_animation.stop()

        width_right = self.word_panel.width()
        width_left = self.segment_panel.width()

        height = max(300, self.height() - 110)
        y = 90

        if visible:
            # Освобождаем место для боковых панелей.
            self.update_content_margins(True)

            left_start = QRectF(
                -width_left - 5, y, width_left, height
            ).toRect()

            left_end = QRectF(
                self.panel_margin, y, width_left, height
            ).toRect()

            right_start = QRectF(
                self.width() + 5, y, width_right, height
            ).toRect()

            right_end = QRectF(
                self.width() - width_right - self.panel_margin,
                y,
                width_right,
                height
            ).toRect()

            self.segment_panel.setGeometry(left_start)
            self.word_panel.setGeometry(right_start)

            self.segment_panel.show()
            self.word_panel.show()

            self.segment_panel.raise_()
            self.word_panel.raise_()

        else:
            left_start = self.segment_panel.geometry()

            left_end = QRectF(
                -width_left - 5, y, width_left, height
            ).toRect()

            right_start = self.word_panel.geometry()

            right_end = QRectF(
                self.width() + 5, y, width_right, height
            ).toRect()

        self.segment_panel_animation.setStartValue(left_start)
        self.segment_panel_animation.setEndValue(left_end)

        self.panel_animation.setStartValue(right_start)
        self.panel_animation.setEndValue(right_end)

        self.segment_panel_animation.start()
        self.panel_animation.start()

    def eventFilter(self, watched, event):
        if not self.isActiveWindow():
            return super().eventFilter(watched, event)

        event_type = event.type()

        if event_type not in (
            QEvent.Type.KeyPress,
            QEvent.Type.KeyRelease,
        ):
            return super().eventFilter(watched, event)

        if event.key() != Qt.Key.Key_Tab:
            return super().eventFilter(watched, event)

        # Игнорируем автоповтор.
        if event.isAutoRepeat():
            return True

        # Нажатие Tab.
        if event_type == QEvent.Type.KeyPress:

            ctrl_pressed = bool(
                event.modifiers()
                & Qt.KeyboardModifier.ControlModifier
            )

            # Ctrl + Tab — переключение закрепления панелей.
            if ctrl_pressed:
                self.ctrl_tab_handled = True

                self.panels_pinned = not self.panels_pinned

                self.toggle_word_panel(
                    self.panels_pinned or self.tab_is_pressed
                )

                return True

            # Обычный Tab — временное открытие панелей.
            self.tab_is_pressed = True

            self.toggle_word_panel(True)

            return True

        # Отпускание Tab.
        elif event_type == QEvent.Type.KeyRelease:

            if self.ctrl_tab_handled:
                self.ctrl_tab_handled = False
                return True

            self.tab_is_pressed = False

            # Закреплённые панели остаются открытыми.
            self.toggle_word_panel(self.panels_pinned)

            return True

        return super().eventFilter(watched, event)

    def _build_ui(self):
        outer_layout = QHBoxLayout(self)
        outer_layout.setContentsMargins(22, 20, 22, 20)

        outer_layout.addStretch(1)

        self.root_layout = QVBoxLayout()
        self.root_layout.setSpacing(14)

        outer_layout.addLayout(self.root_layout, 1)

        outer_layout.addStretch(1)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        # title = QLabel("WhisperX Player")
        # Логотип приложения
        logo_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "Neon_Voicewave_W_Icon.png"
        )

        logo = QLabel()
        pixmap = QPixmap(logo_path)

        if not pixmap.isNull():
            # Обрезаем изображение до центральной квадратной области
            side = min(pixmap.width(), pixmap.height())
            x = (pixmap.width() - side) // 2
            y = (pixmap.height() - side) // 2
            pixmap = pixmap.copy(x, y, side, side)

            logo.setPixmap(
                pixmap.scaled(
                    56, 56,
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation
                )
            )
            logo.setFixedSize(56, 56)
        else:
            logo.setText("WhisperX Player")
            print(f"Не удалось загрузить логотип: {logo_path}")
        
        # ---- Заголовок и подзаголовок ----
        title = QLabel("WhisperX Player")
        title.setObjectName("Title")
        subtitle = QLabel("Синхронизированное аудио и распознанная речь")
        subtitle.setObjectName("Muted")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addWidget(logo)
        header.addLayout(title_box)
        header.addStretch()

        self.open_btn = QPushButton("Открыть аудио")
        self.json_btn = QPushButton("Загрузить JSON")
        self.exit_btn = QPushButton("Выход")
        self.exit_btn.setObjectName("Danger")
        header.addWidget(self.open_btn)
        header.addWidget(self.json_btn)
        header.addWidget(self.exit_btn)
        self.root_layout.addLayout(header)

        panel = QFrame()
        panel.setObjectName("Panel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(14, 12, 14, 12)
        panel_layout.setSpacing(10)

        self.wave = WaveformWidget()
        panel_layout.addWidget(self.wave)

        seek_row = QHBoxLayout()
        self.prev_btn = AnimatedSeekButton("prev")
        self.play_btn = AnimatedPlayButton()
        self.next_btn = AnimatedSeekButton("next")
        self.time_label = QLabel("00:00:00 / 00:00:00")
        self.time_label.setObjectName("Time")
        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 0)
        seek_row.addWidget(self.prev_btn)
        seek_row.addWidget(self.play_btn)
        seek_row.addWidget(self.next_btn)
        seek_row.addWidget(self.seek_slider, 1)
        seek_row.addWidget(self.time_label)
        panel_layout.addLayout(seek_row)

        options = QHBoxLayout()
        options.addWidget(QLabel("Скорость"))
        self.speed_combo = QComboBox()
        for speed in ("0.5×", "0.75×", "0.85×", "1.0×", "1.25×", "1.5×", "2.0×"):
            self.speed_combo.addItem(speed, float(speed.replace("×", "")))
        self.speed_combo.setCurrentIndex(3)
        options.addWidget(self.speed_combo)
        options.addSpacing(18)
        self.volume_label = QLabel("Громкость")
        options.addWidget(self.volume_label)
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setFixedWidth(130)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(85)
        options.addWidget(self.volume_slider)
        options.addStretch()
        self.status = QLabel("Открой аудио и соответствующий JSON WhisperX")
        self.status.setObjectName("Muted")
        options.addWidget(self.status)
        panel_layout.addLayout(options)
        self.root_layout.addWidget(panel)

        self.text = QTextEdit()
        self.text.setReadOnly(True)
        self.text.setPlaceholderText("Загрузи JSON WhisperX — здесь появится текст с подсветкой текущего слова.")
        self.text.setFont(QFont("Noto Sans", 16))
        self.root_layout.addWidget(self.text, 1)

        # Боковая панель информации о слове
        self.word_panel = WordInfoPanel(self)

        self.word_panel.setStyleSheet("""
            QFrame#WordInfoPanel {
                background: #25262d;
                border: 1px solid #414451;
                border-radius: 12px;
            }
        """)

        self.word_panel.setFixedWidth(360)
        self.word_panel.setMinimumHeight(480)
        self.word_panel.adjustSize()

        self.panel_width = 320
        self.panel_margin = 16
        self.panel_visible = False

        self.word_panel.hide()

        # Левая панель перевода сегмента
        self.segment_panel = SegmentTranslationPanel(self)
        self.segment_panel.hide()

        self.segment_panel.adjustSize()

        # Общие параметры боковых панелей
        self.panel_margin = 16
        self.panel_visible = False

        # Анимация левой панели
        self.segment_panel_animation = QPropertyAnimation(
            self.segment_panel, b"geometry", self
        )
        self.segment_panel_animation.setDuration(220)
        self.segment_panel_animation.setEasingCurve(
            QEasingCurve.Type.OutCubic
        )

        # Анимация правой панели
        self.panel_animation = QPropertyAnimation(
            self.word_panel, b"geometry", self
        )
        self.panel_animation.setDuration(220)
        self.panel_animation.setEasingCurve(
            QEasingCurve.Type.OutCubic
        )

        self.panel_animation.finished.connect(
            self.on_panel_animation_finished
        )
        self.segment_panel_animation.finished.connect(
            self.on_panel_animation_finished
        )

    def _connect_signals(self):
        self.open_btn.clicked.connect(self.open_media)
        self.json_btn.clicked.connect(self.open_json)
        self.exit_btn.clicked.connect(self.close)
        self.play_btn.clicked.connect(self.toggle_play)
        self.prev_btn.clicked.connect(self.go_prev_word)
        self.next_btn.clicked.connect(self.go_next_word)
        self.seek_slider.sliderMoved.connect(self.player.setPosition)
        self.wave.seekRequested.connect(self.player.setPosition)
        self.volume_slider.valueChanged.connect(lambda v: self.audio.setVolume(v / 100))
        self.speed_combo.currentIndexChanged.connect(self.change_speed)

        self.player.positionChanged.connect(self.on_position)
        self.player.durationChanged.connect(self.on_duration)
        self.player.playbackStateChanged.connect(self.on_state)
        self.player.errorOccurred.connect(self.on_media_error)

    def _shortcuts(self):
        QShortcut(QKeySequence("Ctrl+Left"), self, activated=self.go_prev_word)
        QShortcut(QKeySequence("Ctrl+Right"), self, activated=self.go_next_word)
        QShortcut(QKeySequence("Space"), self, activated=self.toggle_play)

    def open_media(
        self,
        checked=False,
        path=None,
        restore_position=0
    ):
        if path is None:
            path, _ = QFileDialog.getOpenFileName(
                self,
                "Открыть аудио или видео",
                "",
                "Медиа (*.wav *.flac *.mp3 *.m4a *.aac "
                "*.ogg *.opus *.ts *.mp4 *.mkv);;Все файлы (*)"
            )

        if not path:
            return

        self.media_path = path
        self._restore_position_ms = restore_position

        # Сразу сохраняем путь к медиа.
        self.settings.setValue("media_path", path)
        self.settings.sync()

        self.player.setSource(QUrl.fromLocalFile(path))

        self.wave.set_peaks([])

        self.status.setText("Строю звуковую волну…")

        self.wave_worker = WaveformWorker(path)
        self.wave_worker.ready.connect(self.on_wave_ready)
        self.wave_worker.failed.connect(self.on_wave_failed)
        self.wave_worker.start()

    def on_wave_ready(self, peaks):
        self.wave.set_peaks(peaks)
        self.status.setText("Звуковая волна готова")

    def on_wave_failed(self, message):
        self.status.setText("Звуковая волна недоступна")
        QMessageBox.warning(self, "Waveform", message)

    def open_json(self, checked=False, path=None):

        if path is None:
            path, _ = QFileDialog.getOpenFileName(
                self,
                "Загрузить WhisperX JSON",
                "",
                "JSON (*.json)"
            )

        if not path:
            return

        self.document_generation += 1

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Сегменты WhisperX.
            self.segments = []

            for item in data.get("segments", []):
                text = str(item.get("text", "")).strip()

                try:
                    start = float(item.get("start", 0))
                    end = float(item.get("end", start))
                except (TypeError, ValueError):
                    continue

                if (
                    not text
                    or not math.isfinite(start)
                    or not math.isfinite(end)
                    or end < start
                ):
                    continue

                segment = SegmentSpan(
                    start=start,
                    end=end,
                    text=text,
                    cache_key=""
                )
                segment.cache_key = (
                    TranslationManager.make_cache_key(segment)
                )
                self.segments.append(segment)

            self.segments.sort(key=lambda s: s.start)
            segment_starts = [s.start for s in self.segments]

            
            # Слова WhisperX.
            is_japanese = data.get("language") == "ja"

            if is_japanese:
                # Для японского используем вложенные words
                # каждого сегмента и морфологию Fugashi.
                source_words = rebuild_japanese_word_segments(
                    data.get("segments", []),
                    self.ja_tagger
                )
            else:
                # Для остальных языков сохраняем старую логику.
                source_words = data.get("word_segments") or []

                if not source_words:
                    for segment in data.get("segments", []):
                        source_words.extend(
                            segment.get("words", [])
                        )

            ordered_words = []

            for item in source_words:
                token = str(item.get("word", ""))

                if not token:
                    continue

                try:
                    start = float(item.get("start", 0))
                    end = float(item.get("end", start))
                except (TypeError, ValueError):
                    continue

                if (
                    not math.isfinite(start)
                    or not math.isfinite(end)
                    or end < start
                ):
                    continue

                ordered_words.append((start, end, token))

            ordered_words.sort(key=lambda item: item[0])

            if not ordered_words:
                raise ValueError(
                    "В JSON не найдены корректные word_segments/words."
                )

            self.words = []
            self.word_segment_indices = []

            text_parts = []

            char_pos = 0
            qt_char_pos = 0

            for start, end, token in ordered_words:
                self.words.append(
                    WordSpan(
                        start=start,
                        end=end,
                        char_start=char_pos,
                        char_len=len(token),
                        qt_char_start=qt_char_pos,
                        qt_char_len=utf16_length(token)
                    )
                )

                text_parts.append(token)

                char_pos += len(token)
                qt_char_pos += utf16_length(token)

                segment_idx = -1

                if self.segments:
                    midpoint = (start + end) / 2

                    candidate = (
                        bisect.bisect_right(
                            segment_starts,
                            midpoint
                        ) - 1
                    )

                    if candidate >= 0:
                        segment = self.segments[candidate]

                        if segment.start <= midpoint <= segment.end:
                            segment_idx = candidate

                self.word_segment_indices.append(segment_idx)

            # Индексы начала каждого слова для бинарного поиска.
            self.word_starts = [
                word.start for word in self.words
            ]

            self.text.setPlainText("".join(text_parts))

            self.current_index = -1
            self.current_segment_index = -1

            # Запоминаем JSON текущей сессии.
            self.json_path = path

            self.settings.setValue(
                "json_path",
                path
            )
            self.settings.sync()

            base, _ = os.path.splitext(path)

            self.translation_cache_path = (
                base + ".translations.json"
            )

            self.translation_worker.prepareRequested.emit(
                self.segments,
                self.translation_cache_path
            )

            self.status.setText(
                f"Загружено слов: {len(self.words):,}".replace(",", " ")
            )

            self.update_highlight(
                self.player.position()
            )

        except (
            OSError,
            json.JSONDecodeError,
            TypeError,
            ValueError
        ) as exc:
            QMessageBox.critical(
                self,
                "Ошибка JSON",
                str(exc)
            )

    def change_speed(self, index):
        speed = self.speed_combo.currentData()
        if speed:
            self.player.setPlaybackRate(float(speed))

    def toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def on_state(self, state):
        playing = (
            state == QMediaPlayer.PlaybackState.PlayingState
        )

        self.play_btn.set_playing(playing)

    def on_duration(self, duration):
        self.duration_ms = duration

        self.seek_slider.setRange(
            0,
            max(0, duration)
        )

        # Восстанавливаем позицию после загрузки медиа.
        if self._restore_position_ms > 0 and duration > 0:
            position = min(
                self._restore_position_ms,
                duration
            )

            self.player.setPosition(position)
            self._restore_position_ms = 0

        self.wave.set_playhead(
            self.player.position(),
            duration
        )

        self.update_time()

    def on_position(self, position):
        if not self.seek_slider.isSliderDown():
            self.seek_slider.setValue(position)
        self.wave.set_playhead(position, self.duration_ms)
        self.update_time()
        self.update_highlight(position)

    def update_time(self):
        self.time_label.setText(
            f"{fmt_time(self.player.position())} / {fmt_time(self.duration_ms)}"
        )

    def update_highlight(self, position_ms):
        if not self.words:
            return

        t = position_ms / 1000.0

        idx = bisect.bisect_right(
            self.word_starts, t
        ) - 1

        if (
            idx < 0
            or not (
                self.words[idx].start
                <= t
                < self.words[idx].end
            )
        ):
            idx = -1

        if idx == self.current_index:
            return

        self.current_index = idx

        # Определяем сегмент текущего слова.
        segment_idx = -1

        if idx >= 0 and idx < len(self.word_segment_indices):
            segment_idx = self.word_segment_indices[idx]

        if segment_idx != self.current_segment_index:
            self.current_segment_index = segment_idx
            self.update_segment_panel()

        if idx >= 0:
            word = self.words[idx]

            token = self.text.toPlainText()[
                word.char_start:
                word.char_start + word.char_len
            ].strip()

            if token:
                self.dictionaryLookupRequested.emit(
                    self.document_generation,
                    idx,
                    token
                )

        selections = []

        if idx >= 0:
            word = self.words[idx]

            cursor = self.text.textCursor()

            cursor.setPosition(word.qt_char_start)

            cursor.setPosition(
                word.qt_char_start + word.qt_char_len,
                QTextCursor.MoveMode.KeepAnchor
            )

            selection = QTextEdit.ExtraSelection()
            selection.cursor = cursor
            selection.format.setBackground(
                QColor("#5865f2")
            )
            selection.format.setForeground(
                QColor("#ffffff")
            )

            selections.append(selection)

            self.text.setTextCursor(cursor)
            self.text.ensureCursorVisible()

        self.text.setExtraSelections(selections)

    def go_prev_word(self):
        if not self.words:
            return
        t = self.player.position() / 1000
        idx = bisect.bisect_right(self.word_starts, t) - 1
        if idx >= 0 and t - self.words[idx].start <= 0.2:
            idx -= 1
        self.seek_word(max(0, idx))

    def go_next_word(self):
        if not self.words:
            return
        t = self.player.position() / 1000
        idx = bisect.bisect_right(self.word_starts, t + 0.01)
        self.seek_word(min(len(self.words) - 1, idx))

    def seek_word(self, index):
        if 0 <= index < len(self.words):
            self.player.setPosition(int(self.words[index].start * 1000))

    def on_media_error(self, error, error_string):
        if error != QMediaPlayer.Error.NoError:
            self.status.setText("Ошибка воспроизведения")
            QMessageBox.warning(self, "Ошибка медиа", error_string or "Не удалось открыть файл.")

    def closeEvent(self, event):
        self.save_session()

        self.player.stop()

        if self.wave_worker and self.wave_worker.isRunning():
            self.wave_worker.quit()
            self.wave_worker.wait(1500)

        if hasattr(self, "dictionary_thread"):
            self.dictionary_thread.quit()
            self.dictionary_thread.wait()

        if hasattr(self, "translation_thread"):
            self.translation_thread.quit()
            self.translation_thread.wait()

        super().closeEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyleSheet(APP_QSS)
    window = WhisperPlayer()
    window.show()
    sys.exit(app.exec())
