
import sys

from PySide6.QtCore import Qt, QEvent
from PySide6.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QLabel,
    QTextEdit,
)


class TabTest(QWidget):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("PySide6 — Ctrl+Tab test")
        self.resize(600, 350)

        self.label = QLabel("Нажми Ctrl+Tab или обычный Tab")
        self.log = QTextEdit()
        self.log.setReadOnly(True)

        layout = QVBoxLayout(self)
        layout.addWidget(self.label)
        layout.addWidget(self.log)

        self.ctrl_pressed = False
        self.tab_pinned = False
        self.tab_pressed = False

        QApplication.instance().installEventFilter(self)

    def write_log(self, message):
        self.log.append(message)
        print(message)

    def eventFilter(self, watched, event):
        if event.type() not in (
            QEvent.Type.KeyPress,
            QEvent.Type.KeyRelease,
        ):
            return super().eventFilter(watched, event)

        key_name = Qt.Key(event.key()).name
        event_name = (
            "PRESS"
            if event.type() == QEvent.Type.KeyPress
            else "RELEASE"
        )

        self.write_log(
            f"{event_name}: "
            f"key={key_name}, "
            f"text={event.text()!r}, "
            f"modifiers={event.modifiers()}, "
            f"scan={event.nativeScanCode()}, "
            f"autoRepeat={event.isAutoRepeat()}"
        )

        if event.type() == QEvent.Type.KeyPress:

            if event.key() == Qt.Key.Key_Control:
                self.ctrl_pressed = True

            elif event.key() in (
                Qt.Key.Key_Tab,
                Qt.Key.Key_Backtab,
            ):
                ctrl = (
                    self.ctrl_pressed
                    or bool(
                        event.modifiers()
                        & Qt.KeyboardModifier.ControlModifier
                    )
                )

                if ctrl:
                    if not event.isAutoRepeat():
                        self.tab_pinned = not self.tab_pinned
                        self.label.setText(
                            f"Закрепление: {'ВКЛ' if self.tab_pinned else 'ВЫКЛ'}"
                        )
                        self.write_log(
                            f"*** CTRL+TAB: pinned={self.tab_pinned} ***"
                        )
                    return True

                if not event.isAutoRepeat():
                    self.tab_pressed = True
                    self.label.setText("Обычный Tab: НАЖАТ")
                    self.write_log("*** TAB DOWN ***")

                return True

        elif event.type() == QEvent.Type.KeyRelease:

            if event.key() == Qt.Key.Key_Control:
                self.ctrl_pressed = False

            elif event.key() in (
                Qt.Key.Key_Tab,
                Qt.Key.Key_Backtab,
            ):
                if not self.ctrl_pressed:
                    self.tab_pressed = False
                    self.label.setText(
                        f"Обычный Tab: ОТПУЩЕН | Закрепление: "
                        f"{'ВКЛ' if self.tab_pinned else 'ВЫКЛ'}"
                    )
                    self.write_log("*** TAB UP ***")

                return True

        return super().eventFilter(watched, event)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = TabTest()
    window.show()
    sys.exit(app.exec())
