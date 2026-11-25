from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.tier1.memory import clear_all_memories, delete_memory_by_id, list_memories


class MemoryManagerDialog(QDialog):
    """Manual viewer and deleter for J.U.D.E.'s local SQLite memories."""

    def __init__(self, db_path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.db_path = Path(db_path)
        self.setWindowTitle("J.U.D.E. — Memory Manager")
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setMinimumSize(500, 360)

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
            QLabel#countLbl {
                color: #94A3B8;
                font-size: 12px;
            }
            QListWidget {
                background-color: #1E293B;
                color: #F1F5F9;
                border: 1px solid #334155;
                border-radius: 8px;
                padding: 4px;
                font-size: 12.5px;
            }
            QListWidget::item {
                padding: 6px 8px;
                border-bottom: 1px solid #334155;
            }
            QListWidget::item:selected {
                background-color: #0284C7;
                color: #FFFFFF;
            }
            QPushButton {
                background-color: #1E293B;
                color: #E2E8F0;
                border: 1px solid #475569;
                border-radius: 6px;
                padding: 6px 12px;
                font-weight: 600;
            }
            QPushButton:hover {
                background-color: #334155;
            }
            QPushButton#dangerBtn {
                background-color: #7F1D1D;
                color: #FECACA;
                border: 1px solid #991B1B;
            }
            QPushButton#dangerBtn:hover {
                background-color: #991B1B;
            }
            """
        )

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        top_row = QHBoxLayout()
        title_lbl = QLabel("Saved Local Memories")
        title_lbl.setObjectName("titleLbl")
        self.count_lbl = QLabel("0 saved memories")
        self.count_lbl.setObjectName("countLbl")
        top_row.addWidget(title_lbl, 1)
        top_row.addWidget(self.count_lbl)
        layout.addLayout(top_row)

        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        layout.addWidget(self.list_widget, 1)

        btn_row = QHBoxLayout()
        self.delete_btn = QPushButton("Delete Selected")
        self.delete_btn.clicked.connect(self._on_delete_selected)

        self.clear_btn = QPushButton("Clear All")
        self.clear_btn.setObjectName("dangerBtn")
        self.clear_btn.clicked.connect(self._on_clear_all)

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.reload_memories)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)

        btn_row.addWidget(self.delete_btn)
        btn_row.addWidget(self.clear_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self.refresh_btn)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        self.reload_memories()

    def reload_memories(self) -> None:
        self.list_widget.clear()
        items = list_memories(self.db_path)
        for mem in items:
            item = QListWidgetItem(mem["fact_text"])
            item.setData(Qt.ItemDataRole.UserRole, mem["id"])
            item.setToolTip(f"Key: {mem['key_phrase']} | Saved: {mem['created_at']}")
            self.list_widget.addItem(item)
        self.count_lbl.setText(f"{len(items)} saved memor{'y' if len(items) == 1 else 'ies'}")

    def _on_delete_selected(self) -> None:
        selected = self.list_widget.selectedItems()
        if not selected:
            return
        for item in selected:
            mem_id = item.data(Qt.ItemDataRole.UserRole)
            if mem_id is not None:
                delete_memory_by_id(int(mem_id), db_path=self.db_path)
        self.reload_memories()

    def _on_clear_all(self) -> None:
        if self.list_widget.count() == 0:
            return
        reply = QMessageBox.question(
            self,
            "Clear All Memories",
            "Delete all saved memories from local storage?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            clear_all_memories(db_path=self.db_path)
            self.reload_memories()
