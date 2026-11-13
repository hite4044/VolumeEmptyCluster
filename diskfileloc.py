#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DiskFileLoc：按 NTFS 簇号定位/复制文件。

命令行用法（独立可用，行为与旧版一致）:
    python diskfileloc.py <文件夹> [-o 输出.json] [-v]
    递归遍历文件夹，获取每个文件最后一个簇的簇号（LCN），按簇号从大到小
    排序保存为 JSON 列表：[{"cl": .., "fp": ..}, ...]

本模块同时作为 main.py（PySide6 图形界面）的后端，额外提供:
    - list_volumes() / get_volume_info()  卷枚举、容量、簇大小
    - scan_volume_files()                 扫描卷上全部文件及其最前/最后簇号
    - copy_file()                         流式复制（.part 临时文件 + 原子替换，
                                          可随时中断且不残留半成品，可保留时间，
                                          可选无缓冲读写与多队列并行）
    - set_file_times()                    设置文件的创建/访问/修改时间

通用说明:
- cl/LCN 是逻辑簇号（相对所在卷的卷首），仅对 NTFS 本地卷有意义。
- 空文件、内容常驻 MFT 的极小文件没有簇号（None）。
- 目录符号链接与 junction 不会被跟踪，避免死循环。
- 仅在 Windows 上可用；受保护文件可能需要管理员权限。
"""

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import stat as stat_mod
import sys
import threading
import time
from collections import namedtuple

if os.name != "nt":
    sys.stderr.write("错误：本程序依赖 Windows 的 FSCTL_GET_RETRIEVAL_POINTERS，"
                     "只能在 Windows 上运行。\n")
    sys.exit(2)

# ---------------- Win32 常量 ----------------
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_WRITE_ATTRIBUTES = 0x00000100
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
OPEN_ALWAYS = 3
CREATE_ALWAYS = 2
FILE_BEGIN = 0
FILE_FLAG_NO_BUFFERING = 0x02000000
INVALID_HANDLE_VALUE = wt.HANDLE(-1).value

# CTL_CODE(FILE_DEVICE_FILE_SYSTEM=9, 29, METHOD_NEITHER, FILE_ANY_ACCESS)
FSCTL_GET_RETRIEVAL_POINTERS = 0x00090073
ERROR_MORE_DATA = 234
ERROR_HANDLE_EOF = 38
LCN_HOLE = -1  # 稀疏文件“洞”的 LCN（0xFFFFFFFFFFFFFFFF 按有符号读即 -1）

REPARSE_TAG_SYMLINK = 0xA000000C
REPARSE_TAG_MOUNT_POINT = 0xA0000003
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400

DRIVE_TYPE_NAMES = {
    0: "未知", 1: "无根目录", 2: "可移动磁盘", 3: "本地磁盘",
    4: "网络驱动器", 5: "光驱", 6: "RAM 磁盘",
}

_EPOCH_1601_TO_1970 = 116444736000000000  # FILETIME 纪元差，单位 100ns

# 大文件优化：达到阈值后改用更大的读写块
DEFAULT_LARGE_FILE_THRESHOLD = 100 << 20  # 100MB
LARGE_CHUNK = 16 << 20
NORMAL_CHUNK = 1 << 20


class Extent(ctypes.Structure):
    """RETRIEVAL_POINTERS_BUFFER 中单个 extent：本段 VCN 区间映射到的 LCN。"""
    _fields_ = [("NextVcn", ctypes.c_longlong), ("Lcn", ctypes.c_longlong)]


class RPBHeader(ctypes.Structure):
    _fields_ = [("ExtentCount", wt.DWORD), ("StartingVcn", ctypes.c_longlong)]


_MAX_EXTENTS = ((1 << 20) - ctypes.sizeof(RPBHeader)) // ctypes.sizeof(Extent)


class RPB(ctypes.Structure):
    """RETRIEVAL_POINTERS_BUFFER（extent 数组按最大缓冲区一次性分配，避免碎片文件多次往返）。"""
    _fields_ = [("ExtentCount", wt.DWORD),
                ("StartingVcn", ctypes.c_longlong),
                ("Extents", Extent * _MAX_EXTENTS)]


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wt.DWORD), ("dwHighDateTime", wt.DWORD)]


class OVERLAPPED(ctypes.Structure):
    """定位 I/O 用：仅填写 Offset/OffsetHigh 指定读写位置（同步句柄亦可）。"""
    _fields_ = [("Internal", ctypes.c_void_p),
                ("InternalHigh", ctypes.c_void_p),
                ("Offset", wt.DWORD),
                ("OffsetHigh", wt.DWORD),
                ("hEvent", ctypes.c_void_p)]


_rpb = RPB()
BUF_SIZE = ctypes.sizeof(_rpb)

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateFileW.restype = wt.HANDLE
kernel32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                                 wt.DWORD, wt.DWORD, wt.HANDLE]
kernel32.DeviceIoControl.restype = wt.BOOL
kernel32.DeviceIoControl.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_void_p, wt.DWORD,
                                     ctypes.c_void_p, wt.DWORD,
                                     ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
kernel32.CloseHandle.restype = wt.BOOL
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.GetVolumeInformationW.restype = wt.BOOL
kernel32.GetVolumeInformationW.argtypes = [wt.LPCWSTR, wt.LPWSTR, wt.DWORD,
                                           ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.DWORD),
                                           ctypes.POINTER(wt.DWORD), wt.LPWSTR, wt.DWORD]
kernel32.GetLogicalDriveStringsW.restype = wt.DWORD
kernel32.GetLogicalDriveStringsW.argtypes = [wt.DWORD, wt.LPWSTR]
kernel32.GetDriveTypeW.restype = wt.UINT
kernel32.GetDriveTypeW.argtypes = [wt.LPCWSTR]
kernel32.GetDiskFreeSpaceW.restype = wt.BOOL
kernel32.GetDiskFreeSpaceW.argtypes = [wt.LPCWSTR, ctypes.POINTER(wt.DWORD),
                                       ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.DWORD),
                                       ctypes.POINTER(wt.DWORD)]
kernel32.GetDiskFreeSpaceExW.restype = wt.BOOL
kernel32.GetDiskFreeSpaceExW.argtypes = [wt.LPCWSTR, ctypes.POINTER(ctypes.c_ulonglong),
                                         ctypes.POINTER(ctypes.c_ulonglong),
                                         ctypes.POINTER(ctypes.c_ulonglong)]
kernel32.SetFileTime.restype = wt.BOOL
kernel32.SetFileTime.argtypes = [wt.HANDLE, ctypes.POINTER(FILETIME),
                                 ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME)]
kernel32.ReadFile.restype = wt.BOOL
kernel32.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                              ctypes.POINTER(wt.DWORD), ctypes.POINTER(OVERLAPPED)]
kernel32.WriteFile.restype = wt.BOOL
kernel32.WriteFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                               ctypes.POINTER(wt.DWORD), ctypes.POINTER(OVERLAPPED)]
kernel32.SetFilePointerEx.restype = wt.BOOL
kernel32.SetFilePointerEx.argtypes = [wt.HANDLE, ctypes.c_longlong,
                                      ctypes.POINTER(ctypes.c_longlong), wt.DWORD]
kernel32.SetEndOfFile.restype = wt.BOOL
kernel32.SetEndOfFile.argtypes = [wt.HANDLE]


def long_path(path):
    """加 \\\\?\\ 前缀，突破 MAX_PATH 限制。"""
    p = os.path.abspath(path)
    if p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC" + p[1:]
    return "\\\\?\\" + p


# ---------------- 簇分布查询 ----------------

def _open_layout_handle(path):
    """以“共享一切”的方式打开文件供 FSCTL 查询；出错抛 OSError。"""
    h = kernel32.CreateFileW(path, GENERIC_READ,
                             FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                             None, OPEN_EXISTING, 0, None)
    if not h or h == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    return h


def _query_extents(handle):
    """读取文件全部簇分布，返回 [(start_vcn, next_vcn, lcn)]；lcn 为 None 表示稀疏洞。"""
    extents = []
    start_vcn = 0
    while True:
        in_vcn = ctypes.c_longlong(start_vcn)
        returned = wt.DWORD(0)
        ok = kernel32.DeviceIoControl(handle, FSCTL_GET_RETRIEVAL_POINTERS,
                                      ctypes.byref(in_vcn), ctypes.sizeof(in_vcn),
                                      ctypes.byref(_rpb), BUF_SIZE,
                                      ctypes.byref(returned), None)
        if not ok:
            err = ctypes.get_last_error()
            if err == ERROR_HANDLE_EOF:
                break
            if err != ERROR_MORE_DATA:
                raise ctypes.WinError(err)
        count = min(_rpb.ExtentCount, _MAX_EXTENTS)
        if count == 0:
            break  # 没有分配任何簇
        prev_vcn = _rpb.StartingVcn
        for i in range(count):
            ext = _rpb.Extents[i]
            if ext.NextVcn - prev_vcn > 0:
                extents.append((prev_vcn, ext.NextVcn,
                                None if ext.Lcn == LCN_HOLE else ext.Lcn))
            prev_vcn = ext.NextVcn
        if ok:
            break
        start_vcn = prev_vcn  # 缓冲区装不下（极碎的文件）：从断点继续
    return extents


def get_first_last_cluster_lcn(path):
    """返回文件 (最前簇 LCN, 最后簇 LCN)；文件没有任何簇（空/常驻 MFT）时为 (None, None)。

    出错抛 OSError。
    """
    h = _open_layout_handle(long_path(path))
    try:
        first = last = None
        for start_vcn, next_vcn, lcn in _query_extents(h):
            if lcn is None:
                continue
            if first is None:
                first = lcn
            last = lcn + (next_vcn - start_vcn) - 1
        return first, last
    finally:
        kernel32.CloseHandle(h)


def get_last_cluster_lcn(path):
    """查询文件最后一个已分配簇的 LCN；文件没有任何簇（空/常驻 MFT）时返回 None。

    出错抛 OSError。
    """
    return get_first_last_cluster_lcn(path)[1]


# ---------------- 文件遍历 ----------------

def collect_files(root, warning_cb=None):
    """深度优先遍历（不跟踪目录符号链接/junction），返回所有普通文件的绝对路径列表。

    warning_cb(msg) 用于接收无法进入目录等警告；不传则打印到标准错误。
    """
    def warn(msg):
        if warning_cb is not None:
            warning_cb(msg)
        else:
            print(f"[警告] {msg}", file=sys.stderr)

    files, dirs = [], [os.path.abspath(root)]
    seen = set()
    while dirs:
        cur = dirs.pop()
        key = os.path.normcase(cur)
        if key in seen:
            continue
        seen.add(key)
        try:
            entries = list(os.scandir(cur))
        except OSError as e:
            warn(f"无法进入目录 {cur}：{e}")
            continue
        for entry in entries:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat_mod.S_ISDIR(st.st_mode):
                # 目录链接（symlink/junction）不跟踪，其余 reparse 目录（如 OneDrive）正常进入
                if (st.st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT
                        and getattr(st, "st_reparse_tag", 0)
                        in (REPARSE_TAG_SYMLINK, REPARSE_TAG_MOUNT_POINT)):
                    continue
                dirs.append(entry.path)
            elif stat_mod.S_ISREG(st.st_mode):
                files.append(entry.path)
    return files


# ---------------- 卷信息 ----------------

def get_volume_info(root):
    """返回卷信息 dict（root/label/fs/type_name/cluster_size/total_clusters/free_bytes/total_bytes）。

    root 可以是任意路径，取其所在盘符。信息获取失败的项目为 0/空串。
    """
    drive, _ = os.path.splitdrive(os.path.abspath(root))
    if not drive:
        return None
    root_path = drive + "\\"
    info = {"root": root_path, "label": "", "fs": "",
            "type_code": 0, "type_name": "未知",
            "cluster_size": 0, "total_clusters": 0, "free_clusters": 0,
            "total_bytes": 0, "free_bytes": 0}
    t = kernel32.GetDriveTypeW(ctypes.c_wchar_p(root_path))
    info["type_code"] = int(t)
    info["type_name"] = DRIVE_TYPE_NAMES.get(int(t), "未知")
    vbuf = ctypes.create_unicode_buffer(128)
    fbuf = ctypes.create_unicode_buffer(64)
    if kernel32.GetVolumeInformationW(ctypes.c_wchar_p(root_path),
                                      vbuf, 128, None, None, None, fbuf, 64):
        info["label"] = vbuf.value
        info["fs"] = fbuf.value
    spc, bps, free_c, total_c = wt.DWORD(), wt.DWORD(), wt.DWORD(), wt.DWORD()
    if kernel32.GetDiskFreeSpaceW(ctypes.c_wchar_p(root_path),
                                  ctypes.byref(spc), ctypes.byref(bps),
                                  ctypes.byref(free_c), ctypes.byref(total_c)):
        info["cluster_size"] = spc.value * bps.value
        info["free_clusters"] = free_c.value
        info["total_clusters"] = total_c.value
    free_b, total_b = ctypes.c_ulonglong(), ctypes.c_ulonglong()
    if kernel32.GetDiskFreeSpaceExW(ctypes.c_wchar_p(root_path),
                                    ctypes.byref(free_b), ctypes.byref(total_b), None):
        info["free_bytes"] = free_b.value
        info["total_bytes"] = total_b.value
    return info


def list_volumes():
    """枚举本机可访问的本地卷（跳过网络驱动器与无效盘符），返回 get_volume_info 列表。"""
    n = kernel32.GetLogicalDriveStringsW(0, None)
    buf = ctypes.create_unicode_buffer(n + 4)
    kernel32.GetLogicalDriveStringsW(len(buf), buf)
    letters = ctypes.wstring_at(ctypes.addressof(buf), len(buf)).split("\x00")
    out = []
    for part in letters:
        if not part:
            continue
        t = int(kernel32.GetDriveTypeW(ctypes.c_wchar_p(part)))
        if t in (0, 1, 4):  # 未知 / 无根目录 / 网络驱动器
            continue
        info = get_volume_info(part)
        if info:
            out.append(info)
    return out


# ---------------- 卷扫描 ----------------

FileRec = namedtuple("FileRec", ["path", "size", "first_lcn", "last_lcn"])


def scan_volume_files(root, progress_cb=None, cancel_cb=None, warning_cb=None):
    """扫描 root 下所有普通文件，返回 FileRec 列表；用户取消时返回 None。

    - progress_cb(done, total)：扫描进度；
    - cancel_cb() 返回 True 时尽快中止；
    - warning_cb(msg)：无法进入目录等警告。
    """
    files = collect_files(root, warning_cb=warning_cb)
    out = []
    total = len(files)
    for i, fp in enumerate(files, 1):
        if cancel_cb is not None and cancel_cb():
            return None
        size = 0
        try:
            size = os.stat(fp).st_size
        except OSError:
            pass
        first = last = None
        try:
            first, last = get_first_last_cluster_lcn(fp)
        except OSError:
            pass
        out.append(FileRec(fp, size, first, last))
        if progress_cb is not None and (i % 50 == 0 or i == total):
            progress_cb(i, total)
    return out


# ---------------- 文件复制 ----------------

def _unix_to_filetime(t):
    ts = int(t * 10_000_000) + _EPOCH_1601_TO_1970
    if ts < 1:
        ts = 1
    return FILETIME(ts & 0xFFFFFFFF, (ts >> 32) & 0xFFFFFFFF)


def set_file_times(path, created=None, accessed=None, modified=None):
    """设置文件时间（Unix 秒，Windows 上 created 即创建时间）；None 表示不修改。

    出错抛 OSError。
    """
    h = kernel32.CreateFileW(long_path(path), FILE_WRITE_ATTRIBUTES,
                             FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                             None, OPEN_EXISTING, 0, None)
    if not h or h == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        args = []
        for t in (created, accessed, modified):
            args.append(ctypes.byref(_unix_to_filetime(t)) if t is not None else None)
        if not kernel32.SetFileTime(h, *args):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(h)


def _safe_remove(path):
    try:
        os.remove(long_path(path))
    except OSError:
        pass


def _bytes_per_sector(path):
    """返回 path 所在卷的扇区字节数；查询失败时按最常见的 4096 处理。"""
    drive, _ = os.path.splitdrive(os.path.abspath(path))
    if drive:
        spc, bps, fc, tc = wt.DWORD(), wt.DWORD(), wt.DWORD(), wt.DWORD()
        if kernel32.GetDiskFreeSpaceW(ctypes.c_wchar_p(drive + "\\"),
                                      ctypes.byref(spc), ctypes.byref(bps),
                                      ctypes.byref(fc), ctypes.byref(tc)) and bps.value:
            return bps.value
    return 4096


def _open_win32(path, access, share, flags=0, creation=OPEN_EXISTING):
    h = kernel32.CreateFileW(long_path(path), access, share, None, creation, flags, None)
    if not h or h == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    return h


def _aligned_buffer(n, align):
    """分配 n 字节、起始地址按 align 对齐的缓冲区，返回 (buffer, 对齐后地址)。"""
    if align <= 1:
        buf = ctypes.create_string_buffer(n)
        return buf, ctypes.addressof(buf)
    buf = ctypes.create_string_buffer(n + align)
    addr = ctypes.addressof(buf)
    off = (align - addr % align) % align
    return buf, addr + off


def _make_overlapped(offset):
    ov = OVERLAPPED()
    ov.Offset = offset & 0xFFFFFFFF
    ov.OffsetHigh = (offset >> 32) & 0xFFFFFFFF
    return ov


def _read_at(h, addr, nbytes, offset):
    """定位同步读（OVERLAPPED 仅用于指定偏移），返回实际读取字节数。"""
    got = wt.DWORD(0)
    if kernel32.ReadFile(h, ctypes.c_void_p(addr), nbytes,
                         ctypes.byref(got), ctypes.byref(_make_overlapped(offset))):
        return got.value
    err = ctypes.get_last_error()
    if err == ERROR_HANDLE_EOF:
        return 0
    raise ctypes.WinError(err)


def _write_at(h, addr, nbytes, offset):
    written = wt.DWORD(0)
    if not kernel32.WriteFile(h, ctypes.c_void_p(addr), nbytes,
                              ctypes.byref(written), ctypes.byref(_make_overlapped(offset))):
        raise ctypes.WinError(ctypes.get_last_error())
    if written.value != nbytes:
        raise OSError(f"写入不完整：{written.value}/{nbytes} 字节")


def _truncate_handle(h, size):
    newpos = ctypes.c_longlong(0)
    if not kernel32.SetFilePointerEx(h, ctypes.c_longlong(size),
                                     ctypes.byref(newpos), FILE_BEGIN):
        raise ctypes.WinError(ctypes.get_last_error())
    if not kernel32.SetEndOfFile(h):
        raise ctypes.WinError(ctypes.get_last_error())


def _write_sector_tail(hdst, addr, got, off, align):
    """无缓冲模式：文件尾不足一扇区的部分补零写成整扇区（复制完成后统一截断）。"""
    full = (got // align) * align
    if full:
        _write_at(hdst, addr, full, off)
    rem = got - full
    if rem:
        # 只清零真实数据之后的填充区（[full, got) 是有效数据，不能动）
        ctypes.memset(addr + got, 0, align - rem)
        _write_at(hdst, addr + full, align, off + full)


def _copy_sequential_python(src, tmp, size, chunk, progress_cb, stop_cb):
    """常规缓冲顺序复制（Python 流），返回 'ok'/'stopped'。"""
    with open(long_path(src), "rb") as fsrc, open(long_path(tmp), "wb") as fdst:
        while True:
            if stop_cb is not None and stop_cb():
                return "stopped"
            block = fsrc.read(chunk)
            if not block:
                break
            fdst.write(block)
            if progress_cb is not None:
                progress_cb(fdst.tell(), size)
    return "ok"


def _copy_sequential_win32(src, tmp, size, chunk, progress_cb, stop_cb):
    """无缓冲（FILE_FLAG_NO_BUFFERING）顺序复制到 tmp，返回 'ok'/'stopped'。

    读写请求与缓冲区按源/目标卷扇区大小的较大值对齐；块大小自动调整为
    扇区倍数；文件尾不足一扇区的部分补零写入，收尾时截断到真实大小。
    """
    align = max(_bytes_per_sector(src), _bytes_per_sector(tmp))
    if chunk % align:
        chunk += align - chunk % align
    hsrc = _open_win32(src, GENERIC_READ,
                       FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                       FILE_FLAG_NO_BUFFERING)
    try:
        hdst = _open_win32(tmp, GENERIC_WRITE,
                           FILE_SHARE_READ | FILE_SHARE_WRITE,
                           FILE_FLAG_NO_BUFFERING, CREATE_ALWAYS)
        try:
            if size == 0:
                return "ok"  # CREATE_ALWAYS 已建立空文件
            buf, addr = _aligned_buffer(chunk, align)
            pos = 0
            while pos < size:
                if stop_cb is not None and stop_cb():
                    return "stopped"
                got = _read_at(hsrc, addr, chunk, pos)  # 请求大小须为扇区倍数
                if got == 0:
                    break  # 源文件在复制期间被截断，按已读内容收尾
                _write_sector_tail(hdst, addr, got, pos, align)
                pos += got
                if progress_cb is not None:
                    progress_cb(min(pos, size), size)
            _truncate_handle(hdst, pos)
            return "ok"
        finally:
            kernel32.CloseHandle(hdst)
    finally:
        kernel32.CloseHandle(hsrc)


def _copy_parallel(src, tmp, size, chunk, queues, unbuffered, progress_cb, stop_cb):
    """多队列并行复制大文件到 tmp：每队列独立句柄、按块条带分工、定位 I/O 写入。

    unbuffered=True 时所有句柄带 FILE_FLAG_NO_BUFFERING（对齐要求同上）；
    收尾由主调用方截断到真实大小。返回 'ok'/'stopped'，出错抛 OSError。
    """
    src_bps = _bytes_per_sector(src)
    dst_bps = _bytes_per_sector(tmp)
    align = max(src_bps, dst_bps) if unbuffered else 1
    if unbuffered and chunk % align:
        chunk += align - chunk % align
    flags = FILE_FLAG_NO_BUFFERING if unbuffered else 0
    n_chunks = (size + chunk - 1) // chunk
    n_workers = max(1, min(queues, n_chunks))

    # 先建立临时文件，避免多线程 CREATE_ALWAYS 竞争截断
    h0 = _open_win32(tmp, GENERIC_WRITE,
                     FILE_SHARE_READ | FILE_SHARE_WRITE, flags, CREATE_ALWAYS)
    kernel32.CloseHandle(h0)

    errors = []
    stop_flag = threading.Event()
    counter = [0]
    lock = threading.Lock()

    def run_worker(worker_idx, hsrc, hdst):
        try:
            buf, addr = _aligned_buffer(chunk, align)
            i = worker_idx
            while i < n_chunks:
                if stop_flag.is_set() or (stop_cb is not None and stop_cb()):
                    stop_flag.set()
                    return
                off = i * chunk
                req = chunk if unbuffered else min(chunk, size - off)
                got = _read_at(hsrc, addr, req, off)
                if got == 0:
                    return  # 源文件被截断等异常情况
                if unbuffered:
                    _write_sector_tail(hdst, addr, got, off, align)
                else:
                    _write_at(hdst, addr, got, off)
                with lock:
                    counter[0] += got
                    done = counter[0]
                if progress_cb is not None:
                    progress_cb(min(done, size), size)
                i += n_workers
        except Exception as e:
            errors.append(e)
            stop_flag.set()
        finally:
            kernel32.CloseHandle(hdst)
            kernel32.CloseHandle(hsrc)

    workers = []
    try:
        for j in range(n_workers):
            hsrc = _open_win32(src, GENERIC_READ,
                               FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, flags)
            try:
                hdst = _open_win32(tmp, GENERIC_WRITE,
                                   FILE_SHARE_READ | FILE_SHARE_WRITE, flags, OPEN_ALWAYS)
            except OSError:
                kernel32.CloseHandle(hsrc)
                raise
            t = threading.Thread(target=run_worker, args=(j, hsrc, hdst))
            t.start()
            workers.append(t)
        for t in workers:
            t.join()
    finally:
        for t in workers:  # 异常路径也等待线程结束，确保清理临时文件时句柄已关闭
            t.join(timeout=30)
    if errors:
        raise errors[0]
    if stop_flag.is_set():
        return "stopped"
    if unbuffered:  # 截断补零写出的文件尾
        h = _open_win32(tmp, GENERIC_WRITE,
                        FILE_SHARE_READ | FILE_SHARE_WRITE, 0, OPEN_EXISTING)
        try:
            _truncate_handle(h, size)
        finally:
            kernel32.CloseHandle(h)
    return "ok"


def copy_file(src, dst, *, preserve_times=True,
              large_file_threshold=DEFAULT_LARGE_FILE_THRESHOLD,
              large_chunk=LARGE_CHUNK, queues=1, skip_cache=False,
              progress_cb=None, stop_cb=None):
    """流式复制 src → dst：先写临时文件 dst.part，全部写完后原子替换。

    - 随时中断：stop_cb() 返回 True 时尽快中断并返回 'stopped'，
      临时文件被删除，目标目录不残留半成品；出错同样清理后抛 OSError；
    - progress_cb(copied_bytes, total_bytes)：每写完一块回调一次；
    - 大文件优化：大小 ≥ large_file_threshold（None 表示禁用）的文件使用
      large_chunk 大块读写，其余用 1MB 块；
    - queues > 1 时对大文件启用多队列并行读写（每队列独立句柄，按块条带
      分工，定位 I/O 写入）；
    - skip_cache=True 时使用 FILE_FLAG_NO_BUFFERING 无缓冲读写（绕过系统
      缓存；块大小自动对齐到卷扇区大小，文件尾补零写入后截断）；
    - preserve_times=True 时保留源文件的创建、修改、访问时间。

    返回 'ok' 或 'stopped'。
    """
    st = os.stat(src)
    size = max(st.st_size, 0)
    large = large_file_threshold is not None and size >= large_file_threshold
    chunk = max(large_chunk, 1) if large else NORMAL_CHUNK
    use_parallel = large and queues > 1 and size > 0
    tmp = dst + ".part"
    dst_dir = os.path.dirname(dst)
    if dst_dir:
        os.makedirs(long_path(dst_dir), exist_ok=True)
    finished = False
    try:
        if use_parallel:
            result = _copy_parallel(src, tmp, size, chunk, max(1, int(queues)),
                                    skip_cache, progress_cb, stop_cb)
        elif skip_cache:
            result = _copy_sequential_win32(src, tmp, size, chunk, progress_cb, stop_cb)
        else:
            result = _copy_sequential_python(src, tmp, size, chunk, progress_cb, stop_cb)
        if result == "stopped":
            return "stopped"
        os.replace(long_path(tmp), long_path(dst))
        finished = True
        if preserve_times:
            try:
                set_file_times(dst, created=st.st_ctime,
                               accessed=st.st_atime, modified=st.st_mtime)
            except OSError:
                pass  # 时间保留是尽力而为
        return "ok"
    finally:
        if not finished:
            _safe_remove(tmp)


# ---------------- 命令行入口（保持原有行为） ----------------

def scan_files(files, verbose=False):
    results = []
    failed = 0
    total = len(files)
    t0 = time.monotonic()
    progress_every = 200
    for idx, fp in enumerate(files, 1):
        lcn = None
        try:
            lcn = get_last_cluster_lcn(long_path(fp))
        except OSError as e:
            failed += 1
            if verbose:
                print(f"[失败] {fp}：{e}", file=sys.stderr)
        results.append({"cl": lcn, "fp": fp})
        if total > progress_every and idx % progress_every == 0:
            print(f"\r  已处理 {idx}/{total} ...", end="", flush=True, file=sys.stderr)
    if total > progress_every:
        print(file=sys.stderr)
    return results, failed, time.monotonic() - t0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="diskfileloc",
        description="递归遍历文件夹，获取每个文件最后一个簇的簇号（NTFS LCN），"
                    "按簇号从大到小排序并保存为 JSON 列表：[{\"cl\": .., \"fp\": ..}, ...]")
    parser.add_argument("folder", help="要遍历的文件夹")
    parser.add_argument("-o", "--output", default="diskfileloc.json",
                        help="输出 JSON 文件路径（默认 ./diskfileloc.json，传 - 输出到标准输出）")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="打印打开失败的文件及其原因")
    args = parser.parse_args(argv)

    root = os.path.abspath(args.folder)
    if not os.path.isdir(root):
        print(f"错误：{args.folder} 不是有效文件夹", file=sys.stderr)
        return 2

    # 卷文件系统检查（簇号仅 NTFS 上有意义）
    drive, _ = os.path.splitdrive(root)
    fs_name = ctypes.create_unicode_buffer(64)  # 第 7 个参数才是文件系统名缓冲区
    if (drive and kernel32.GetVolumeInformationW(ctypes.c_wchar_p(drive + "\\"),
                                                 None, 0, None, None, None,
                                                 fs_name, 64)
            and fs_name.value.upper() != "NTFS"):
        print(f"[警告] {drive} 的文件系统是 {fs_name.value}，不是 NTFS，查询结果可能无效",
              file=sys.stderr)

    print(f"正在遍历 {root} ...", file=sys.stderr)
    files = collect_files(root)

    out_is_stdout = args.output == "-"
    if not out_is_stdout:  # 避免把输出文件自身（旧一次运行的结果）也统计进去
        out_abs = os.path.normcase(os.path.abspath(args.output))
        files = [f for f in files if os.path.normcase(f) != out_abs]

    print(f"共 {len(files)} 个文件，开始查询簇号 ...", file=sys.stderr)
    try:
        results, failed, elapsed = scan_files(files, verbose=args.verbose)
    except KeyboardInterrupt:
        print("\n已中断，未生成输出。", file=sys.stderr)
        return 130

    # 按最后一个簇号从大到小排序；无簇号（null）的排最后
    results.sort(key=lambda r: (r["cl"] is None, -r["cl"] if r["cl"] is not None else 0))

    payload = json.dumps(results, ensure_ascii=False, indent=2)
    if out_is_stdout:
        sys.stdout.buffer.write((payload + "\n").encode("utf-8"))
    else:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(payload + "\n")

    no_cluster = sum(1 for r in results if r["cl"] is None)
    print(f"完成：共 {len(results)} 个文件，其中 {len(results) - no_cluster} 个有簇号、"
          f"{no_cluster - failed} 个无簇（空文件/常驻 MFT）、{failed} 个查询失败（cl=null），"
          f"耗时 {elapsed:.1f} 秒", file=sys.stderr)
    if not out_is_stdout:
        print(f"结果已写入 {os.path.abspath(args.output)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
