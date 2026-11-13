#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DiskFileLoc GUI —— 单面板完成：选择源卷 → 设置参数 → 开始/停止 → 双进度条展示。

功能:
- 按“从后面（最后簇号从大到小）/ 从前面（最前簇号从小到大）”的顺序，
  把卷中的文件带目录结构复制到目标文件夹；
- 双进度条：总体（累计大小 + 文件数 + 速度）与当前文件；Windows 任务栏同步显示进度；
- 大文件优化：≥100MB（阈值与块大小可调）的文件使用大块读写；可设置多队列
  并行读写同一个大文件；可选跳过缓存（FILE_FLAG_NO_BUFFERING 无缓冲读写）；
- 可选“删除源文件”：每个文件复制成功后删除源文件（开始前二次确认）；
- 可随时停止，不残留复制了一半的文件（.part 临时文件自动清理）；
- 跳过扫描后已不存在的源文件；可保留文件的创建/修改/访问时间；
- 停止条件：卷后部/前部已处理空白空间达到阈值（每完成一个文件后，按该文件
  的最前/最后簇判断）、累计复制数据量上限；
- 界面自动跟随系统暗黑模式（Qt >= 6.5 时实时跟随，含注册表兜底）。

运行: .venv\\Scripts\\python main.py    （后端见 diskfileloc.py）
"""

import ctypes
import os
import sys
import time
from ctypes import HRESULT, POINTER, WINFUNCTYPE, byref, c_ulong, c_ulonglong, c_void_p

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QRadioButton, QSpinBox,
    QVBoxLayout, QWidget,
)

import diskfileloc as backend

MB = 1024 ** 2
GB = 1024 ** 3


def fmt_size(n):
    x = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if x < 1024 or unit == "PB":
            return f"{x:.0f} {unit}" if unit == "B" else f"{x:.2f} {unit}"
        x /= 1024


def fmt_speed(bps):
    return f"{fmt_size(bps)}/s" if bps >= 0 else "—"


def fmt_elapsed(t):
    if t < 60:
        return f"{t:.1f} 秒"
    return f"{int(t // 60)} 分 {t % 60:.0f} 秒"


# ---------------- 暗黑模式适配 ----------------

def _registry_dark_mode():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            value, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
            return value == 0
    except OSError:
        return False


def system_uses_dark_mode(app=None):
    if app is not None:
        try:
            scheme = app.styleHints().colorScheme()
            if scheme != Qt.ColorScheme.Unknown:  # Qt >= 6.5，由系统实时给出
                return scheme == Qt.ColorScheme.Dark
        except Exception:
            pass
    return _registry_dark_mode()


def build_dark_palette():
    p = QPalette()
    fg = QColor(230, 230, 230)
    p.setColor(QPalette.Window, QColor(53, 53, 53))
    p.setColor(QPalette.WindowText, fg)
    p.setColor(QPalette.Base, QColor(35, 35, 35))
    p.setColor(QPalette.AlternateBase, QColor(45, 45, 45))
    p.setColor(QPalette.ToolTipBase, QColor(45, 45, 45))
    p.setColor(QPalette.ToolTipText, fg)
    p.setColor(QPalette.Text, fg)
    p.setColor(QPalette.Button, QColor(53, 53, 53))
    p.setColor(QPalette.ButtonText, fg)
    p.setColor(QPalette.BrightText, QColor(255, 80, 80))
    p.setColor(QPalette.Link, QColor(42, 130, 218))
    p.setColor(QPalette.Highlight, QColor(42, 130, 218))
    p.setColor(QPalette.HighlightedText, QColor(240, 240, 240))
    p.setColor(QPalette.PlaceholderText, QColor(130, 130, 130))
    disabled = QColor(110, 110, 110)
    for group in (QPalette.Disabled,):
        p.setColor(group, QPalette.WindowText, disabled)
        p.setColor(group, QPalette.Text, disabled)
        p.setColor(group, QPalette.ButtonText, disabled)
    return p


def apply_theme(app):
    app.setStyle("Fusion")
    if system_uses_dark_mode(app):
        app.setPalette(build_dark_palette())
    else:
        app.setPalette(app.style().standardPalette())


# ---------------- Windows 任务栏进度（ITaskbarList3，PySide6 无 QtWinExtras，直接走 COM） ----------------

class TaskbarProgress:
    """把进度映射到任务栏按钮上；初始化失败时静默降级为空操作。"""

    _CLSID = "{56FDF344-FD6D-11d0-958A-006097C9A090}"  # CLSID_TaskbarList
    _IID = "{EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF}"    # IID_ITaskbarList3
    CLSCTX_INPROC_SERVER = 0x1

    TBPF_NOPROGRESS = 0x0
    TBPF_INDETERMINATE = 0x1
    TBPF_NORMAL = 0x2
    TBPF_ERROR = 0x4
    TBPF_PAUSED = 0x8

    def __init__(self, hwnd):
        self._ok = False
        try:
            self._hwnd = c_void_p(int(hwnd))
            ole32 = ctypes.OleDLL("ole32")
            clsid = ctypes.GUID(self._CLSID)
            iid = ctypes.GUID(self._IID)
            ptr = c_void_p()
            try:
                ole32.CoCreateInstance(byref(clsid), None, self.CLSCTX_INPROC_SERVER,
                                       byref(iid), byref(ptr))
            except OSError:  # COM 尚未初始化时兜底
                ole32.CoInitialize(None)
                ole32.CoCreateInstance(byref(clsid), None, self.CLSCTX_INPROC_SERVER,
                                       byref(iid), byref(ptr))
            self._ptr = ptr
            # ITaskbarList3 虚表槽位：0..2 IUnknown，3..7 ITaskbarList，8 ITaskbarList2，
            # 9 SetProgressValue，10 SetProgressState
            vtbl = ctypes.cast(ptr, POINTER(POINTER(c_void_p))).contents
            self._hr_init = WINFUNCTYPE(HRESULT, c_void_p)(vtbl[3].value)
            self._set_value = WINFUNCTYPE(HRESULT, c_void_p, c_ulonglong, c_ulonglong)(vtbl[9].value)
            self._set_state = WINFUNCTYPE(HRESULT, c_void_p, c_ulong)(vtbl[10].value)
            self._release = WINFUNCTYPE(HRESULT, c_void_p)(vtbl[2].value)
            self._hr_init(self._ptr)
            self._ok = True
        except Exception:
            self._ok = False

    def set_value(self, completed, total):
        if self._ok and total > 0:
            try:
                self._set_value(self._ptr, self._hwnd, min(completed, total), total)
            except Exception:
                pass

    def set_state(self, state):
        if self._ok:
            try:
                self._set_state(self._ptr, self._hwnd, state)
            except Exception:
                pass

    def close(self):
        if self._ok:
            try:
                self._release(self._ptr)
            except Exception:
                pass
            self._ok = False


# ---------------- 后台工作线程 ----------------

class CopyWorker(QThread):
    phase_sig = Signal(str)
    scan_sig = Signal(int, int)      # 扫描进度 done, total
    overall_sig = Signal(dict)       # 总进度
    file_sig = Signal(dict)          # 当前文件进度
    log_sig = Signal(str)
    done_sig = Signal(dict)          # 汇总

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._stop = False
        self._stop_reason = ""
        self._copied_total = 0   # 已完成文件的累计字节
        self._cur_copied = 0     # 当前文件已写字节
        self._cur_name = ""
        self._cur_total = 0
        self._file_t0 = 0.0
        self._copy_t0 = 0.0
        self._last_emit = 0.0
        self._total_bytes = 0
        self._files_total = 0
        self._files_done = 0
        self._missing = 0
        self._failed = 0
        self._deleted = 0

    def stop(self):
        self._stop = True

    # ---------- 停止条件 ----------
    def _data_limit_reached(self):
        limit = self.cfg.get("data_limit_bytes") or 0
        return bool(limit) and self._copied_total + self._cur_copied >= limit

    def _should_stop(self):
        if self._stop:
            self._stop_reason = self._stop_reason or "用户停止"
            return True
        if self._data_limit_reached():
            self._stop_reason = self._stop_reason or "已达数据量上限"
            return True
        return False

    def _check_space_threshold(self, rec, order, cluster_size, total_clusters, thr):
        """刚完成一个文件后调用：通过该文件的最前/最后簇统计卷后部/前部
        已复制覆盖的空白空间，达到阈值则停止。返回 (是否停止, 提示)。"""
        if cluster_size <= 0:
            return False, ""
        if order == "back":
            # 从后面开始：卷末到该文件最前簇之间的后部区域已被处理覆盖
            pos = rec.first_lcn
            if pos is None:
                return False, ""
            covered = (total_clusters - pos) * cluster_size
            side = "后部"
        else:
            # 从前面开始：卷首到该文件最后簇之间的前部区域已被处理覆盖
            pos = rec.last_lcn
            if pos is None:
                return False, ""
            covered = (pos + 1) * cluster_size
            side = "前部"
        if covered >= thr:
            return True, (f"卷{side}已复制覆盖的空白空间 {fmt_size(covered)} "
                          f"达到阈值 {fmt_size(thr)}，停止。")
        return False, ""

    # ---------- 进度信号 ----------
    def _tick(self, force=False):
        now = time.monotonic()
        if not force and now - self._last_emit < 0.1:
            return
        self._last_emit = now
        copied = self._copied_total + self._cur_copied
        elapsed = max(now - self._copy_t0, 1e-6)
        self.overall_sig.emit({
            "copied": copied, "total": self._total_bytes,
            "files_done": self._files_done, "files_total": self._files_total,
            "speed": copied / elapsed,
        })
        if self._cur_name:
            fel = max(now - self._file_t0, 1e-6)
            self.file_sig.emit({
                "name": self._cur_name, "copied": self._cur_copied,
                "total": self._cur_total, "speed": self._cur_copied / fel,
            })

    def _on_chunk(self, copied, total):
        self._cur_copied = copied
        self._cur_total = total
        self._tick()

    # ---------- 主流程 ----------
    def run(self):
        summary = {"stopped": False, "reason": "", "copied_files": 0, "missing": 0,
                   "failed": 0, "deleted": 0, "bytes": 0, "elapsed": 0.0, "scanned": 0}
        t0 = time.monotonic()
        try:
            self._pipeline(summary)
        except Exception as e:  # 兜底：保证 UI 一定能收到 done
            self.log_sig.emit(f"[错误] 发生未预期的异常：{e!r}")
            summary["stopped"] = True
            summary["reason"] = f"内部错误：{e!r}"
        finally:
            summary.update(copied_files=self._files_done, missing=self._missing,
                           failed=self._failed, deleted=self._deleted,
                           bytes=self._copied_total,
                           elapsed=time.monotonic() - t0)
            self.done_sig.emit(summary)

    def _pipeline(self, summary):
        cfg = self.cfg
        root = cfg["root"]

        # 1. 扫描卷
        self.phase_sig.emit("正在扫描卷上的文件 ...")
        recs = backend.scan_volume_files(
            root,
            progress_cb=lambda done, total: self.scan_sig.emit(done, total),
            cancel_cb=lambda: self._stop,
            warning_cb=lambda msg: self.log_sig.emit(f"[警告] {msg}"),
        )
        if recs is None:
            summary["stopped"] = True
            summary["reason"] = "用户停止"
            return
        summary["scanned"] = len(recs)
        self.log_sig.emit(f"扫描完成：共 {len(recs)} 个文件")

        # 2. 排序（无簇号的文件排在最后）
        if cfg["order"] == "back":
            recs.sort(key=lambda r: (r.last_lcn is None, -(r.last_lcn or 0),
                                     -(r.first_lcn or 0), r.path))
            self.log_sig.emit("顺序：从后面开始（按最后簇号从大到小）")
        else:
            recs.sort(key=lambda r: (r.first_lcn is None, r.first_lcn or 0,
                                     r.last_lcn or 0, r.path))
            self.log_sig.emit("顺序：从前面开始（按最前簇号从小到大）")

        self._total_bytes = sum(r.size for r in recs)
        self._files_total = len(recs)
        self._copy_t0 = time.monotonic()

        # 3. 空间阈值所需的卷参数
        thr = cfg.get("space_limit_bytes") or 0
        cluster_size = total_clusters = 0
        if thr:
            vol = backend.get_volume_info(root)
            cluster_size = vol["cluster_size"]
            total_clusters = vol["total_clusters"]
            if cluster_size <= 0:
                self.log_sig.emit("[警告] 无法获取卷簇大小，空间阈值停止条件不会生效")

        self.phase_sig.emit("正在复制 ...")
        self._tick(force=True)

        # 4. 逐文件复制
        for rec in recs:
            if self._should_stop():
                summary["stopped"] = True
                summary["reason"] = self._stop_reason
                break

            src = rec.path
            if not os.path.exists(src):
                self._missing += 1
                self._total_bytes = max(self._total_bytes - rec.size, 0)
                self.log_sig.emit(f"[跳过] 源文件已不存在：{src}")
                self._tick()
                continue

            rel = os.path.relpath(src, root)
            dst = os.path.join(cfg["target"], rel)
            self._cur_name = rel
            self._cur_total = rec.size
            self._cur_copied = 0
            self._file_t0 = time.monotonic()
            if rec.size >= (1 << 20):
                self._tick(force=True)  # 大文件立即在界面上切换“当前文件”显示

            try:
                result = backend.copy_file(
                    src, dst,
                    preserve_times=cfg["preserve_times"],
                    large_file_threshold=cfg["large_threshold_bytes"],
                    large_chunk=cfg["large_chunk_bytes"],
                    queues=cfg["queues"],
                    skip_cache=cfg["skip_cache"],
                    progress_cb=self._on_chunk,
                    stop_cb=self._should_stop,
                )
                if result == "stopped":
                    summary["stopped"] = True
                    summary["reason"] = self._stop_reason or "用户停止"
                    break
                self._files_done += 1
                self._copied_total += self._cur_total
                if cfg.get("delete_source"):
                    try:
                        os.remove(src)
                        self._deleted += 1
                    except OSError as e:
                        self.log_sig.emit(f"[警告] 删除源文件失败：{src}：{e}")
                if thr:  # 通过刚完成的文件判断卷后部/前部已处理空白空间是否达到阈值
                    stop, msg = self._check_space_threshold(
                        rec, cfg["order"], cluster_size, total_clusters, thr)
                    if stop:
                        self.log_sig.emit(msg)
                        summary["stopped"] = True
                        summary["reason"] = "卷后部/前部空白空间达到阈值"
                        break
            except FileNotFoundError:
                self._missing += 1
                self._total_bytes = max(self._total_bytes - rec.size, 0)
                self.log_sig.emit(f"[跳过] 源文件已不存在：{src}")
            except OSError as e:
                self._failed += 1
                self._total_bytes = max(self._total_bytes - max(rec.size - self._cur_copied, 0), 0)
                self.log_sig.emit(f"[失败] {src}：{e}")
            finally:
                self._cur_copied = 0
                self._cur_name = ""
                self._tick()

        if summary["stopped"]:
            self.phase_sig.emit(f"已停止：{summary['reason']}")
        else:
            self.phase_sig.emit("完成")
        self._tick(force=True)


# ---------------- 主窗口（单面板） ----------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.worker = None
        self.taskbar = None
        self.setWindowTitle("DiskFileLoc — 按簇号顺序复制卷文件")
        self.resize(860, 720)
        self._build_ui()
        self.refresh_volumes()

    # ---------- 界面构建 ----------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        lay = QVBoxLayout(central)

        # --- 源卷 ---
        row = QHBoxLayout()
        row.addWidget(QLabel("源卷："))
        self.vol_combo = QComboBox()
        self.vol_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.vol_combo.setMinimumContentsLength(20)
        row.addWidget(self.vol_combo, 1)
        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.clicked.connect(self.refresh_volumes)
        row.addWidget(self.refresh_btn)
        lay.addLayout(row)

        # --- 目标文件夹 ---
        row = QHBoxLayout()
        row.addWidget(QLabel("目标文件夹："))
        self.target_edit = QLineEdit()
        self.target_edit.setPlaceholderText("选择一个用于存放复制结果的目录")
        row.addWidget(self.target_edit, 1)
        browse = QPushButton("浏览...")
        browse.clicked.connect(self._choose_target)
        row.addWidget(browse)
        lay.addLayout(row)

        # --- 顺序 ---
        row = QHBoxLayout()
        row.addWidget(QLabel("复制顺序："))
        self.rb_back = QRadioButton("从后面（按最后簇号 从大到小）")
        self.rb_back.setChecked(True)
        self.rb_front = QRadioButton("从前面（按最前簇号 从小到大）")
        row.addWidget(self.rb_back)
        row.addWidget(self.rb_front)
        row.addStretch(1)
        lay.addLayout(row)

        # --- 复制选项 ---
        row = QHBoxLayout()
        self.chk_times = QCheckBox("保留文件时间（创建/修改/访问）")
        self.chk_times.setChecked(True)
        row.addWidget(self.chk_times)
        row.addStretch(1)
        self.chk_large = QCheckBox("大文件优化：不小于")
        self.chk_large.setChecked(True)
        self.chk_large.setToolTip("勾选后，达到阈值的文件使用下方块大小读写，其余文件使用 1MB 块。")
        self.large_spin = QDoubleSpinBox()
        self.large_spin.setRange(1, 1000000)
        self.large_spin.setValue(100)
        self.large_spin.setSuffix(" MB")
        row.addWidget(self.chk_large)
        row.addWidget(self.large_spin)
        row.addWidget(QLabel("块大小"))
        self.blk_spin = QDoubleSpinBox()
        self.blk_spin.setRange(1, 1024)
        self.blk_spin.setValue(16)
        self.blk_spin.setSuffix(" MB")
        self.blk_spin.setToolTip("大文件每块读写的字节数；跳过缓存时会自动对齐到卷扇区大小。")
        row.addWidget(self.blk_spin)
        self.chk_large.toggled.connect(self.blk_spin.setEnabled)
        lay.addLayout(row)

        row = QHBoxLayout()
        self.chk_del_src = QCheckBox("删除源文件（复制成功后）")
        self.chk_del_src.setToolTip("每个文件复制成功后立即删除源文件；\n"
                                    "停止或复制失败的文件不会被删除。开始前会再次确认。")
        row.addWidget(self.chk_del_src)
        self.chk_skip_cache = QCheckBox("跳过缓存（无缓冲读写）")
        self.chk_skip_cache.setToolTip("使用 FILE_FLAG_NO_BUFFERING 绕过系统缓存直读写磁盘，\n"
                                       "适合大文件顺序复制；块大小会自动对齐到卷扇区大小。")
        row.addWidget(self.chk_skip_cache)
        row.addStretch(1)
        row.addWidget(QLabel("多队列（大文件并行读写）"))
        self.queue_spin = QSpinBox()
        self.queue_spin.setRange(1, 32)
        self.queue_spin.setValue(1)
        self.queue_spin.setToolTip("对达到大文件阈值的文件启用 N 路并行读写队列；\n"
                                   "1 表示关闭（顺序复制）。")
        row.addWidget(self.queue_spin)
        lay.addLayout(row)

        # --- 停止条件 ---
        grp = QGroupBox("停止条件")
        form = QFormLayout(grp)
        self.chk_space = QCheckBox("按簇号位置：卷后部/前部已处理空白空间达到")
        self.chk_space.setToolTip(
            "从后面开始时：每完成一个文件，统计卷后部已复制覆盖的区域"
            "（总簇数 − 该文件最前簇，按簇大小折算）；\n"
            "从前面开始时：统计卷前部已复制覆盖的区域（该文件最后簇 + 1）。\n"
            "达到设定值即停止；触发停止的最后一个文件已完整保留。")
        self.space_spin = QDoubleSpinBox()
        self.space_spin.setRange(0.1, 1000000)
        self.space_spin.setValue(10)
        self.space_spin.setSuffix(" GB")
        form.addRow(self.chk_space, self.space_spin)
        self.chk_data = QCheckBox("累计复制数据量超过")
        self.data_spin = QDoubleSpinBox()
        self.data_spin.setRange(0.1, 1000000)
        self.data_spin.setValue(50)
        self.data_spin.setSuffix(" GB")
        form.addRow(self.chk_data, self.data_spin)
        lay.addWidget(grp)

        # --- 按钮 ---
        row = QHBoxLayout()
        self.start_btn = QPushButton("开始复制")
        self.start_btn.clicked.connect(self.start)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop)
        row.addWidget(self.start_btn)
        row.addWidget(self.stop_btn)
        row.addStretch(1)
        lay.addLayout(row)

        # --- 双进度条 ---
        self.overall_bar = QProgressBar()
        self.overall_label = QLabel("总进度：—")
        self.file_bar = QProgressBar()
        self.file_label = QLabel("当前文件：—")
        lay.addWidget(self.overall_bar)
        lay.addWidget(self.overall_label)
        lay.addWidget(self.file_bar)
        lay.addWidget(self.file_label)

        self.status_label = QLabel("就绪")
        lay.addWidget(self.status_label)

        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumBlockCount(2000)
        lay.addWidget(self.log_edit, 1)

    # ---------- 卷列表 ----------
    def refresh_volumes(self):
        try:
            vols = backend.list_volumes()
        except Exception as e:
            QMessageBox.critical(self, "错误", f"枚举卷失败：{e}")
            return
        self.vol_combo.clear()
        for v in vols:
            text = (f"{v['root']}  [{v['type_name']}]  {v['fs'] or '未知FS'}  "
                    f"标签:{v['label'] or '—'}  总 {fmt_size(v['total_bytes'])} / "
                    f"空闲 {fmt_size(v['free_bytes'])}")
            self.vol_combo.addItem(text, v)

    def _choose_target(self):
        d = QFileDialog.getExistingDirectory(
            self, "选择目标文件夹", self.target_edit.text().strip() or "")
        if d:
            self.target_edit.setText(d)

    # ---------- 运行控制 ----------
    def _set_running(self, running):
        for w in (self.vol_combo, self.refresh_btn, self.target_edit,
                  self.rb_back, self.rb_front, self.chk_times, self.chk_large,
                  self.large_spin, self.blk_spin, self.chk_del_src,
                  self.chk_skip_cache, self.queue_spin,
                  self.chk_space, self.space_spin,
                  self.chk_data, self.data_spin, self.start_btn):
            w.setEnabled(not running)
        self.stop_btn.setEnabled(running)

    def _ensure_taskbar(self):
        if self.taskbar is None:
            self.taskbar = TaskbarProgress(self.winId())

    def start(self):
        if self.worker is not None:
            return
        vol = self.vol_combo.currentData()
        if vol is None:
            QMessageBox.warning(self, "提示", "请先选择一个源卷。")
            return
        target = self.target_edit.text().strip()
        if not target:
            QMessageBox.warning(self, "提示", "请先选择目标文件夹。")
            return
        target_abs = os.path.abspath(target)
        if not os.path.isdir(target_abs):
            r = QMessageBox.question(
                self, "目标文件夹不存在", f"{target_abs}\n不存在，是否创建？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if r != QMessageBox.StandardButton.Yes:
                return
            try:
                os.makedirs(target_abs)
            except OSError as e:
                QMessageBox.critical(self, "错误", f"创建目标文件夹失败：{e}")
                return
        if os.path.normcase(target_abs).startswith(os.path.normcase(vol["root"])):
            r = QMessageBox.warning(
                self, "警告", "目标文件夹位于源卷内，可能造成嵌套复制。\n确定继续吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if r != QMessageBox.StandardButton.Yes:
                return
        if (vol["fs"] or "").upper() != "NTFS":
            r = QMessageBox.warning(
                self, "警告",
                f"源卷文件系统为 {vol['fs'] or '未知'}（非 NTFS），无法获取簇号，"
                "将按路径顺序复制。\n确定继续吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if r != QMessageBox.StandardButton.Yes:
                return
        if self.chk_del_src.isChecked():
            r = QMessageBox.warning(
                self, "确认删除源文件",
                "已勾选“删除源文件”：每个文件复制成功后将立即从源卷删除源文件，\n"
                "此操作不可恢复（停止或复制失败的文件不会被删除）。确定继续吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if r != QMessageBox.StandardButton.Yes:
                return

        order = "back" if self.rb_back.isChecked() else "front"
        cfg = {
            "root": vol["root"],
            "target": target_abs,
            "order": order,
            "preserve_times": self.chk_times.isChecked(),
            "large_threshold_bytes": (int(self.large_spin.value() * MB)
                                      if self.chk_large.isChecked() else None),
            "large_chunk_bytes": int(self.blk_spin.value() * MB),
            "queues": self.queue_spin.value(),
            "skip_cache": self.chk_skip_cache.isChecked(),
            "delete_source": self.chk_del_src.isChecked(),
            "space_limit_bytes": (int(self.space_spin.value() * GB)
                                  if self.chk_space.isChecked() else 0),
            "data_limit_bytes": (int(self.data_spin.value() * GB)
                                 if self.chk_data.isChecked() else 0),
        }
        self.log_edit.appendPlainText(
            f"=== 开始：{vol['root']} → {target_abs}（{'从后面' if order == 'back' else '从前面'}） ===")
        self.overall_bar.setValue(0)
        self.file_bar.setValue(0)
        self.overall_label.setText("总进度：—")
        self.file_label.setText("当前文件：—")

        w = CopyWorker(cfg)
        w.phase_sig.connect(self.on_phase)
        w.scan_sig.connect(self.on_scan)
        w.overall_sig.connect(self.on_overall)
        w.file_sig.connect(self.on_file)
        w.log_sig.connect(self.on_log)
        w.done_sig.connect(self.on_done)
        self.worker = w
        self._set_running(True)
        self._ensure_taskbar()
        self.taskbar.set_state(TaskbarProgress.TBPF_NORMAL)
        w.start()

    def stop(self):
        if self.worker is not None:
            self.stop_btn.setEnabled(False)
            self.status_label.setText("正在停止 ...")
            self.taskbar.set_state(TaskbarProgress.TBPF_PAUSED)
            self.worker.stop()

    # ---------- 信号槽 ----------
    def on_phase(self, text):
        self.status_label.setText(text)

    def on_scan(self, done, total):
        self.status_label.setText(f"正在扫描：{done}/{total} 个文件")
        self._ensure_taskbar()
        self.taskbar.set_value(done, max(total, 1))

    def on_overall(self, d):
        total = d["total"]
        copied = d["copied"]
        pct = (copied / total * 100) if total > 0 else 100.0
        self.overall_bar.setValue(int(min(pct, 100)))
        self.overall_label.setText(
            f"总进度：{fmt_size(copied)} / {fmt_size(total)}（{pct:.1f}%）   "
            f"文件 {d['files_done']}/{d['files_total']}   速度 {fmt_speed(d['speed'])}")
        self._ensure_taskbar()
        self.taskbar.set_value(copied, max(total, 1))

    def on_file(self, d):
        total = d["total"]
        copied = d["copied"]
        pct = (copied / total * 100) if total > 0 else 100.0
        self.file_bar.setValue(int(min(pct, 100)))
        self.file_label.setText(
            f"当前文件：{d['name']}   {fmt_size(copied)} / {fmt_size(total)}   "
            f"速度 {fmt_speed(d['speed'])}")

    def on_log(self, msg):
        self.log_edit.appendPlainText(msg)

    def on_done(self, s):
        w = self.worker
        if w is not None:
            w.wait()
            w.deleteLater()
            self.worker = None
        self._set_running(False)
        if s["stopped"]:
            self.status_label.setText(f"已停止：{s['reason']}")
        else:
            self.status_label.setText("完成")
        self.log_edit.appendPlainText(
            f"=== {'已停止' if s['stopped'] else '完成'} ===\n"
            f"扫描 {s['scanned']} 个文件；复制 {s['copied_files']} 个，"
            f"累计 {fmt_size(s['bytes'])}；跳过不存在 {s['missing']} 个，"
            f"失败 {s['failed']} 个，删除源文件 {s['deleted']} 个；"
            f"耗时 {fmt_elapsed(s['elapsed'])}")
        if self.taskbar is not None:
            self.taskbar.set_state(TaskbarProgress.TBPF_NOPROGRESS)

    # ---------- 退出 ----------
    def closeEvent(self, event):
        w = self.worker
        if w is not None and w.isRunning():
            r = QMessageBox.question(
                self, "退出", "复制任务正在进行，确定停止并退出？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if r != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            w.stop()
            w.wait(30000)
        if self.taskbar is not None:
            self.taskbar.set_state(TaskbarProgress.TBPF_NOPROGRESS)
            self.taskbar.close()
        event.accept()


def main():
    app = QApplication(sys.argv)
    apply_theme(app)
    # 系统主题切换时实时跟随（Qt >= 6.5；低版本仅在启动时检测）
    try:
        app.styleHints().colorSchemeChanged.connect(lambda _cs: apply_theme(app))
    except Exception:
        pass
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
