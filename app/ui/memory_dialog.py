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
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.tier1.memory import (
    clear_activity_log,
    clear_all_memories,
    clear_all_preferences,
    delete_memory_by_id,
    delete_preference_by_id,
    get_frequent_activities,
    list_memories,
    list_preferences,
)


class MemoryManagerDialog(QDialog):
    """Manual viewer and deleter for J.U.D.E.'s local SQLite memories and learned preferences."""

    def __init__(self, db_path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.db_path = Path(db_path)
        self.setWindowTitle("J.U.D.E. — Memory & Habits Manager")
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setMinimumSize(540, 400)

        self.setStyleSheet(
            """
            QDialog {
                background-color: #0F172A;
                color: #F8FAFC;
                border: 1px solid #334155;
            }
            QTabWidget::pane {
                border: 1px solid #334155;
                border-radius: 8px;
                background-color: #0F172A;
            }
            QTabBar::tab {
                background-color: #1E293B;
                color: #CBD5E1;
                border: 1px solid #334155;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
                padding: 6px 14px;
                margin-right: 4px;
                font-weight: 600;
            }
            QTabBar::tab:selected {
                background-color: #0284C7;
                color: #FFFFFF;
                border-color: #38BDF8;
            }
            QLabel#titleLbl {
                font-size: 15px;
                font-weight: 700;
                color: #F8FAFC;
            }
            QLabel#countLbl, QLabel#subLbl {
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

        root = QVBoxLayout(self)
        root.setSpacing(10)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        # Tab 1: Saved Memories (keeps backward-compatible widget attribute names)
        mem_tab = QWidget()
        mem_layout = QVBoxLayout(mem_tab)
        mem_layout.setSpacing(8)

        top_row = QHBoxLayout()
        title_lbl = QLabel("Saved Local Memories")
        title_lbl.setObjectName("titleLbl")
        self.count_lbl = QLabel("0 saved memories")
        self.count_lbl.setObjectName("countLbl")
        top_row.addWidget(title_lbl, 1)
        top_row.addWidget(self.count_lbl)
        mem_layout.addLayout(top_row)

        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        mem_layout.addWidget(self.list_widget, 1)

        btn_row = QHBoxLayout()
        self.delete_btn = QPushButton("Delete Selected")
        self.delete_btn.clicked.connect(self._on_delete_selected)

        self.clear_btn = QPushButton("Clear All")
        self.clear_btn.setObjectName("dangerBtn")
        self.clear_btn.clicked.connect(self._on_clear_all)

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.reload_all)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)

        btn_row.addWidget(self.delete_btn)
        btn_row.addWidget(self.clear_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self.refresh_btn)
        btn_row.addWidget(close_btn)
        mem_layout.addLayout(btn_row)

        # Tab 2: Learned Preferences & Frequent Habits
        pref_tab = QWidget()
        pref_layout = QVBoxLayout(pref_tab)
        pref_layout.setSpacing(8)

        pref_top = QHBoxLayout()
        pref_title = QLabel("Learned Preferences & Frequent Habits")
        pref_title.setObjectName("titleLbl")
        self.pref_count_lbl = QLabel("0 preferences")
        self.pref_count_lbl.setObjectName("countLbl")
        pref_top.addWidget(pref_title, 1)
        pref_top.addWidget(self.pref_count_lbl)
        pref_layout.addLayout(pref_top)

        self.pref_list_widget = QListWidget()
        self.pref_list_widget.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        pref_layout.addWidget(self.pref_list_widget, 1)

        habits_hdr = QLabel("Most Frequent Targets (Local Activity Log):")
        habits_hdr.setObjectName("subLbl")
        pref_layout.addWidget(habits_hdr)

        self.habit_list_widget = QListWidget()
        self.habit_list_widget.setMaximumHeight(110)
        pref_layout.addWidget(self.habit_list_widget)

        pref_btn_row = QHBoxLayout()
        self.pref_delete_btn = QPushButton("Delete Selected Pref")
        self.pref_delete_btn.clicked.connect(self._on_delete_selected_pref)

        self.pref_clear_btn = QPushButton("Clear Prefs & Habits")
        self.pref_clear_btn.setObjectName("dangerBtn")
        self.pref_clear_btn.clicked.connect(self._on_clear_all_prefs)

        pref_refresh_btn = QPushButton("Refresh")
        pref_refresh_btn.clicked.connect(self.reload_all)

        pref_close_btn = QPushButton("Close")
        pref_close_btn.clicked.connect(self.accept)

        pref_btn_row.addWidget(self.pref_delete_btn)
        pref_btn_row.addWidget(self.pref_clear_btn)
        pref_btn_row.addStretch(1)
        pref_btn_row.addWidget(pref_refresh_btn)
        pref_btn_row.addWidget(pref_close_btn)
        pref_layout.addLayout(pref_btn_row)

        self.tabs.addTab(mem_tab, "Saved Memories")
        self.tabs.addTab(pref_tab, "Learned Preferences & Habits")

        self.reload_all()

    def reload_all(self) -> None:
        self.reload_memories()
        self.reload_preferences()

    def reload_memories(self) -> None:
        self.list_widget.clear()
        items = list_memories(self.db_path)
        for mem in items:
            item = QListWidgetItem(mem["fact_text"])
            item.setData(Qt.ItemDataRole.UserRole, mem["id"])
            item.setToolTip(f"Key: {mem['key_phrase']} | Saved: {mem['created_at']}")
            self.list_widget.addItem(item)
        self.count_lbl.setText(f"{len(items)} saved memor{'y' if len(items) == 1 else 'ies'}")

    def reload_preferences(self) -> None:
        self.pref_list_widget.clear()
        prefs = list_preferences(self.db_path)
        for pref in prefs:
            item = QListWidgetItem(f"{pref['pref_key']}: {pref['pref_value']} ({pref['source']})")
            item.setData(Qt.ItemDataRole.UserRole, pref["id"])
            item.setToolTip(f"Updated: {pref['updated_at']}")
            self.pref_list_widget.addItem(item)
        self.pref_count_lbl.setText(f"{len(prefs)} preference{'s' if len(prefs) != 1 else ''}")

        self.habit_list_widget.clear()
        habits = get_frequent_activities(self.db_path, limit=15)
        for h in habits:
            self.habit_list_widget.addItem(f"[{h['action_type']}] {h['target']} — {h['count']}x")

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

    def _on_delete_selected_pref(self) -> None:
        selected = self.pref_list_widget.selectedItems()
        if not selected:
            return
        for item in selected:
            pref_id = item.data(Qt.ItemDataRole.UserRole)
            if pref_id is not None:
                delete_preference_by_id(int(pref_id), db_path=self.db_path)
        self.reload_preferences()

    def _on_clear_all_prefs(self) -> None:
        if self.pref_list_widget.count() == 0 and self.habit_list_widget.count() == 0:
            return
        reply = QMessageBox.question(
            self,
            "Clear Preferences & Habits",
            "Delete all learned preferences and activity history from local storage?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            clear_all_preferences(db_path=self.db_path)
            clear_activity_log(db_path=self.db_path)
            self.reload_preferences()
