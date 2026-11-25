from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.engine import PendingConfirmation


class ConfirmationDialog(QDialog):
    """Confirmation and diff preview modal shown before modifying files or documents."""

    def __init__(self, pending: PendingConfirmation, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.pending = pending
        self.escalate_requested = False

        self.setWindowTitle(pending.title)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setMinimumWidth(520 if pending.kind in {"word_edit", "word_create", "vmware_cmd"} else 420)

        self.setStyleSheet(
            """
            QDialog {
                background-color: #0F172A;
                color: #F8FAFC;
                border: 1px solid #334155;
            }
            QLabel#titleLbl {
                font-size: 15px;
                font-weight: 700;
                color: #F8FAFC;
            }
            QLabel#pillLbl {
                background-color: #78350F;
                color: #FDE68A;
                border-radius: 9px;
                padding: 3px 10px;
                font-size: 11px;
                font-weight: 600;
            }
            QLabel#metaLbl {
                color: #94A3B8;
                font-size: 12px;
            }
            QPlainTextEdit {
                background-color: #1E293B;
                color: #E2E8F0;
                border: 1px solid #334155;
                border-radius: 6px;
                padding: 6px;
                font-family: "Segoe UI", sans-serif;
                font-size: 12px;
            }
            QPushButton {
                background-color: #1E293B;
                color: #E2E8F0;
                border: 1px solid #475569;
                border-radius: 6px;
                padding: 7px 14px;
                font-weight: 600;
            }
            QPushButton:hover {
                background-color: #334155;
            }
            QPushButton#confirmBtn {
                background-color: #0EA5E9;
                color: #082F49;
                border: none;
            }
            QPushButton#confirmBtn:hover {
                background-color: #38BDF8;
            }
            """
        )

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        top_row = QHBoxLayout()
        title_lbl = QLabel(pending.title)
        title_lbl.setObjectName("titleLbl")
        pill_lbl = QLabel(f"{pending.intent} · {pending.confidence:.0%}")
        pill_lbl.setObjectName("pillLbl")
        top_row.addWidget(title_lbl, 1)
        top_row.addWidget(pill_lbl)
        layout.addLayout(top_row)

        summary_lbl = QLabel(pending.summary)
        summary_lbl.setWordWrap(True)
        layout.addWidget(summary_lbl)

        if pending.target_path:
            target_lbl = QLabel(f"Target: {pending.target_path}")
            target_lbl.setObjectName("metaLbl")
            target_lbl.setWordWrap(True)
            layout.addWidget(target_lbl)

        if pending.backup_note and pending.kind != "vmware_cmd":
            backup_lbl = QLabel(pending.backup_note)
            backup_lbl.setObjectName("metaLbl")
            backup_lbl.setWordWrap(True)
            layout.addWidget(backup_lbl)

        self.after_edit: QPlainTextEdit | None = None
        if pending.kind == "word_edit":
            before_lbl = QLabel("Original Text (Before):")
            before_lbl.setObjectName("metaLbl")
            before_box = QPlainTextEdit(pending.before_text)
            before_box.setReadOnly(True)
            before_box.setMaximumHeight(110)

            after_lbl = QLabel("Proposed Revision (After — editable):")
            after_lbl.setObjectName("metaLbl")
            self.after_edit = QPlainTextEdit(pending.after_text)
            self.after_edit.setMaximumHeight(130)

            layout.addWidget(before_lbl)
            layout.addWidget(before_box)
            layout.addWidget(after_lbl)
            layout.addWidget(self.after_edit)
        elif pending.kind == "word_create":
            content_lbl = QLabel("Proposed Document Content (editable):")
            content_lbl.setObjectName("metaLbl")
            self.after_edit = QPlainTextEdit(pending.after_text)
            self.after_edit.setMaximumHeight(180)
            layout.addWidget(content_lbl)
            layout.addWidget(self.after_edit)
        elif pending.kind == "vmware_cmd":
            cmd_lbl = QLabel("Linux Command to Execute (key-based SSH):")
            cmd_lbl.setObjectName("metaLbl")
            cmd_box = QPlainTextEdit(pending.backup_note or str(pending.payload.get("command", "")))
            cmd_box.setReadOnly(True)
            cmd_box.setMaximumHeight(90)
            cmd_box.setStyleSheet("font-family: 'Consolas', monospace; font-size: 12px;")
            layout.addWidget(cmd_lbl)
            layout.addWidget(cmd_box)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        confirm_btn = QPushButton("Confirm & Execute" if pending.kind in {"vmware_cmd", "close_app"} else "Confirm & Apply")
        confirm_btn.setObjectName("confirmBtn")
        confirm_btn.setDefault(True)
        confirm_btn.clicked.connect(self.accept)
        btn_row.addWidget(confirm_btn)

        layout.addLayout(btn_row)

    def _on_escalate(self) -> None:
        self.escalate_requested = True
        self.reject()

    def edited_after_text(self) -> str | None:
        if self.after_edit is None:
            return None
        return self.after_edit.toPlainText().strip()
