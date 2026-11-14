#!/usr/bin/env python3
"""probe_drive.py — 探测光驱是否真的接受"降速 + 加强重试"指令。

这是 cdrescue 项目的可行性实验（第 0 步）：
  1. 用 SPTI 直接发 READ(10)，绕过 Windows 缓存，逐段测每个区域的真实读取耗时
  2. 用 IOCTL_CDROM_SET_SPEED 把转速压到 1x
  3. 用 MODE SELECT(10) 写 Mode Page 0x01（Read/Write Error Recovery）：
     AWRE/ARRE/TB 打开、read retry count 拉到 255
  4. 再跑一遍同样的测点，对比"默认 vs 加强"

如果第 4 步明显快于第 1 步 → 这个光驱"听话"，cdrescue 的技术前提成立。
如果毫无变化 → 固件忽略了这些参数，项目价值只剩"Windows 版 ddrescue"。

用法:
  python probe_drive.py F:                # 默认 12 个测点
  python probe_drive.py F: --points 24
  python probe_drive.py F: --json out.json

只读：只发 READ 和设置类命令，不写入光盘。
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import json
import sys
import time

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3

IOCTL_CDROM_GET_DRIVE_GEOMETRY = 0x00020000
IOCTL_CDROM_SET_SPEED = 0x00024010
IOCTL_SCSI_PASS_THROUGH_DIRECT = 0x0004D014

CDROM_SET_SPEED = 0
CDROM_SET_SPIN_DOWN = 1
CDROM_CURRENT_SPEED = 2


class SCSI_PASS_THROUGH_DIRECT(ctypes.Structure):
    _fields_ = [
        ("Length", wt.USHORT),
        ("ScsiStatus", ctypes.c_ubyte),
        ("PathId", ctypes.c_ubyte),
        ("TargetId", ctypes.c_ubyte),
        ("Lun", ctypes.c_ubyte),
        ("CdbLength", ctypes.c_ubyte),
        ("SenseInfoLength", ctypes.c_ubyte),
        ("DataIn", ctypes.c_ubyte),
        ("DataTransferLength", wt.ULONG),
        ("TimeOutValue", wt.ULONG),
        ("DataBuffer", ctypes.c_void_p),
        ("SenseInfoOffset", wt.ULONG),
        ("Cdb", ctypes.c_ubyte * 16),
    ]


class DISK_GEOMETRY(ctypes.Structure):
    _fields_ = [
        ("Cylinders", ctypes.c_longlong),
        ("MediaType", wt.DWORD),
        ("TracksPerCylinder", wt.DWORD),
        ("SectorsPerTrack", wt.DWORD),
        ("BytesPerSector", wt.DWORD),
    ]


class CDROM_SET_SPEED_STRUCT(ctypes.Structure):
    _fields_ = [("RequestType", wt.USHORT), ("ReadSpeed", wt.USHORT), ("WriteSpeed", wt.USHORT)]


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateFileW.restype = wt.HANDLE
kernel32.DeviceIoControl.restype = wt.BOOL


def open_drive(letter):
    path = "\\\\.\\" + letter.strip().rstrip("\\/")
    if not path.endswith(":"):
        path += ":"
    # SPTI 的 IOCTL 带 FILE_WRITE_ACCESS 位，句柄必须同时有读写权限
    h = kernel32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
                             None, OPEN_EXISTING, 0, None)
    if h == wt.HANDLE(-1).value or h is None:
        raise OSError(f"打不开 {path}: {ctypes.get_last_error()}")
    return h, path


MEM_COMMIT, MEM_RESERVE, PAGE_READWRITE, MEM_RELEASE = 0x1000, 0x2000, 0x04, 0x8000
kernel32.VirtualAlloc.restype = ctypes.c_void_p


def spti(h, cdb, data_len, timeout_s=20, data_in=True):
    """发一条 SCSI 命令，返回 (scsi_status, sense, elapsed_ms, data)"""
    raw = kernel32.VirtualAlloc(None, max(data_len, 1) + 4096,
                                MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE)
    buf = ctypes.cast(raw, ctypes.POINTER(ctypes.c_ubyte * (max(data_len, 1) + 64))).contents
    s = SCSI_PASS_THROUGH_DIRECT()
    s.Length = ctypes.sizeof(SCSI_PASS_THROUGH_DIRECT)
    s.CdbLength = 10
    s.SenseInfoLength = 0
    s.DataIn = 1 if data_in else 0
    s.DataTransferLength = data_len
    s.TimeOutValue = timeout_s
    s.DataBuffer = ctypes.c_void_p(raw)
    s.SenseInfoOffset = 0
    for i, b in enumerate(cdb[:10]):
        s.Cdb[i] = b

    ret = wt.DWORD(0)
    t0 = time.perf_counter()
    ok = kernel32.DeviceIoControl(h, IOCTL_SCSI_PASS_THROUGH_DIRECT,
                                  ctypes.byref(s), ctypes.sizeof(s),
                                  ctypes.byref(s), ctypes.sizeof(s),
                                  ctypes.byref(ret), None)
    ms = (time.perf_counter() - t0) * 1000
    if not ok:
        kernel32.VirtualFree(ctypes.c_void_p(raw), 0, MEM_RELEASE)
        return -1, b"", ms, b""
    data = ctypes.string_at(raw, data_len)
    kernel32.VirtualFree(ctypes.c_void_p(raw), 0, MEM_RELEASE)
    return s.ScsiStatus, b"", ms, data


def read12(h, lba, sectors=32, timeout_s=20):
    """READ(12)。实测这块光驱对 READ(10) 返回 ERROR_SEM_TIMEOUT，READ(12) 正常。"""
    cdb = [0xA8, 0,
           (lba >> 24) & 0xFF, (lba >> 16) & 0xFF, (lba >> 8) & 0xFF, lba & 0xFF,
           (sectors >> 24) & 0xFF, (sectors >> 16) & 0xFF,
           (sectors >> 8) & 0xFF, sectors & 0xFF, 0]
    return spti(h, cdb, sectors * 2048, timeout_s)


def read_capacity10(h):
    """SPTI READ CAPACITY(10)：返回 (last_lba, block_size)"""
    status, sense, ms, data = spti(h, [0x25, 0, 0, 0, 0, 0, 0, 0, 0, 0], 8, timeout_s=15)
    if status != 0 or len(data) < 8:
        return None
    last = int.from_bytes(data[0:4], "big")
    blk = int.from_bytes(data[4:8], "big")
    return last, blk


def get_geometry(h):
    g = DISK_GEOMETRY()
    ret = wt.DWORD(0)
    ok = kernel32.DeviceIoControl(h, IOCTL_CDROM_GET_DRIVE_GEOMETRY, None, 0,
                                  ctypes.byref(g), ctypes.sizeof(g),
                                  ctypes.byref(ret), None)
    if not ok:
        return None
    total = g.Cylinders * g.TracksPerCylinder * g.SectorsPerTrack
    return dict(bytes_per_sector=g.BytesPerSector, total_sectors=total)


def set_speed(h, kb_per_sec, write_kb=0, req=CDROM_SET_SPEED):
    st = CDROM_SET_SPEED_STRUCT(req, kb_per_sec, write_kb)
    ret = wt.DWORD(0)
    ok = kernel32.DeviceIoControl(h, IOCTL_CDROM_SET_SPEED, ctypes.byref(st), ctypes.sizeof(st),
                                  None, 0, ctypes.byref(ret), None)
    return bool(ok), ctypes.get_last_error()


def get_speed(h):
    st = CDROM_SET_SPEED_STRUCT(CDROM_CURRENT_SPEED, 0, 0)
    ret = wt.DWORD(0)
    ok = kernel32.DeviceIoControl(h, IOCTL_CDROM_SET_SPEED, ctypes.byref(st), ctypes.sizeof(st),
                                  ctypes.byref(st), ctypes.sizeof(st), ctypes.byref(ret), None)
    return (st.ReadSpeed if ok else None), bool(ok)


def set_mode_page_1(h, retry_count=0xFF, awre=1, arre=1, tb=1, per=0, eec=0):
    """MODE SELECT(10)，写 Read/Write Error Recovery 页（page 0x01）"""
    flags = (awre << 7) | (arre << 6) | (tb << 5) | (eec << 4) | (per << 3)
    page = bytes([0x01, 0x0A, flags, retry_count, 0, 0, 0, 0, 0, 0, 0, 0])
    header = bytes(8)          # MODE SELECT(10) 参数头：8 字节全 0
    param = header + page
    cdb = [0x55, 0x10, 0, 0, 0, 0, 0, (len(param) >> 8) & 0xFF, len(param) & 0xFF, 0]
    status, sense, ms, _ = spti(h, cdb, 0, timeout_s=10, data_in=False)
    if status not in (0, -1):
        # 若设备报 ILLEGAL REQUEST，退化为 6 字节 CDB 的 MODE SELECT(6)
        cdb6 = [0x15, 0x10, 0, 0, len(param) & 0xFF, 0]
        status6, sense6, ms6, _ = spti(h, cdb6, 0, timeout_s=10, data_in=False)
        return dict(method="MODE_SELECT_6", status=status6, sense=sense6.hex(), ms=round(ms6, 1))
    return dict(method="MODE_SELECT_10", status=status, sense=sense.hex(), ms=round(ms, 1))


def scan(h, total, points, sectors=32, timeout_s=15):
    """在 total 个扇区上均匀取 points 个测点，逐点 READ(10)，记录耗时"""
    rows = []
    step = max(1, total // points)
    for i in range(points):
        lba = min(total - sectors - 1, i * step)
        status, sense, ms, data = read12(h, lba, sectors, timeout_s)
        kbps = (sectors * 2048 / 1024) / (ms / 1000) if ms > 0 and status == 0 else 0
        rows.append(dict(lba=lba, pct=round(100 * lba / total, 1), status=status,
                         ms=round(ms, 1), kb_per_s=round(kbps, 1),
                         sense=sense[:8].hex() if status else ""))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("drive", nargs="?", default="F:")
    ap.add_argument("--points", type=int, default=12)
    ap.add_argument("--sectors", type=int, default=32, help="每次读多少扇区(32=64KB)")
    ap.add_argument("--json", help="把结果写到文件")
    a = ap.parse_args()

    h, path = open_drive(a.drive)
    geo = get_geometry(h)
    print(f"设备: {path}")
    print(f"几何(IOCTL): {geo}  last_error={ctypes.get_last_error()}")
    cap = read_capacity10(h)
    print(f"READ CAPACITY(10): {cap}")
    if geo:
        total = geo["total_sectors"]
    elif cap:
        total = cap[0] + 1
        geo = dict(bytes_per_sector=cap[1], total_sectors=total, source="READ_CAPACITY")
    else:
        print("拿不到容量信息，退出")
        return 1
    print(f"总扇区: {total} (~{total*2048/1073741824:.2f} GB)")

    sp0, ok0 = get_speed(h)
    print(f"当前转速: {sp0} KB/s (读取成功={ok0})   [DVD 1x ≈ 1385 KB/s]")

    print(f"\n[第 1 轮] 默认参数，{a.points} 个测点…")
    base = scan(h, total, a.points, a.sectors)

    print("[设置] 1x 转速 + Mode Page 1 (retry=255, AWRE/ARRE/TB on)")
    ok_speed, err_speed = set_speed(h, 1385)
    print(f"  SET_SPEED 1x -> {'ok' if ok_speed else 'failed err=' + str(err_speed)}")
    mp = set_mode_page_1(h)
    print(f"  MODE SELECT -> {mp}")
    sp1, ok1 = get_speed(h)
    print(f"  设置后报告转速: {sp1} KB/s")
    time.sleep(1)

    print(f"\n[第 2 轮] 加强参数，同样 {a.points} 个测点…")
    tuned = scan(h, total, a.points, a.sectors)

    print("\n  LBA        位置    默认(ms)   加强(ms)   默认KB/s   加强KB/s  状态")
    better = same = worse = 0
    for b, t in zip(base, tuned):
        bs, ts = b["status"], t["status"]
        mark = ""
        if bs == 0 and ts == 0:
            if t["ms"] < b["ms"] * 0.8:
                mark, better = "✓ 快", better + 1
            elif t["ms"] > b["ms"] * 1.2:
                mark, worse = "✗ 慢", worse + 1
            else:
                mark, same = "= 持平", same + 1
        elif bs != 0 and ts == 0:
            mark, better = "✓ 读出了", better + 1
        elif bs == 0 and ts != 0:
            mark, worse = "✗ 反而失败", worse + 1
        else:
            mark, same = "两轮都失败", same + 1
        print(f"  {b['lba']:>9} {b['pct']:>6.1f}%  {b['ms']:>9.1f} {t['ms']:>9.1f} "
              f"{b['kb_per_s']:>9.1f} {t['kb_per_s']:>9.1f}  {mark}")

    print(f"\n结论: 变快/救回 {better} 个 · 持平 {same} 个 · 变慢/失败 {worse} 个")

    settings_ok = ok_speed and mp.get("status") == 0
    if not settings_ok:
        print("⚠ 注意：转速/重试参数【未被光驱接受】（SET_SPEED 或 MODE SELECT 失败），")
        print("  所以上面两轮的差异只是测量噪声，不能当成'加强参数更有效'的证据。")
    if better > worse and settings_ok:
        print("→ 这个光驱【听】这些参数，cdrescue 的路线成立。")
    elif better == 0 and worse == 0:
        print("→ 没有任何变化：固件忽略转速/重试参数，只剩 ddrescue 式多轮重扫可做。")
    elif not settings_ok:
        print("→ 参数设置失败，本轮无法判断；请把 JSON 贴到 issue 里，这是有价值的硬件数据。")
    else:
        print("→ 结果混合，需要更多测点确认。")

    if a.json:
        json.dump(dict(drive=path, geometry=geo, speed_before=sp0, speed_after=sp1,
                       mode_page=mp, baseline=base, tuned=tuned),
                  open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"结果已写入 {a.json}")

    kernel32.CloseHandle(h)
    return 0


if __name__ == "__main__":
    sys.exit(main())
