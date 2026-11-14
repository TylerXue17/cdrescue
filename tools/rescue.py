#!/usr/bin/env python3
"""rescue.py — 从读不出来的光盘里尽可能救回数据，并产出一份坏块地图。

设计（三个 pass）:
  pass 1  BULK   顺序大块读(默认 1MB)，速度优先，失败块记录下来
  pass 2  RETRY  只针对失败块：块大小逐级缩小(256K→64K→8K→单扇区)，
                 每个大小正反两个方向各试 N 次，同一块读两次一致才算成功
  pass 3  REPORT 连续坏区合并成 LBA 区间，写 badmap.json + 打印汇总

输出:
  out.iso        尽量完整的光盘镜像（读不出的部分填 0）
  out.badmap.json 坏区列表，可交给播放器/编码器做"跳过"而不是"崩溃"
  out.map        进度文件，中断后 --resume 接着跑

绝不写入光盘。所有写操作只发生在输出目录。

用法:
  python rescue.py F: -o D:\\disc.iso
  python rescue.py F: -o D:\\disc.iso --start-mb 2900 --length-mb 200   # 只啃边界区
  python rescue.py F: -o D:\\disc.iso --resume
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from probe_drive import open_drive, read_capacity10, read12, spti, kernel32  # noqa: E402

SECTOR = 2048
FILE_BEGIN = 0

ReadFile = kernel32.ReadFile
ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
SetFilePointerEx = kernel32.SetFilePointerEx


def seek(h, offset):
    li = ctypes.c_longlong(offset)
    return bool(SetFilePointerEx(h, li, None, FILE_BEGIN))


def read_at(h, offset, size):
    """从设备偏移处读 size 字节，返回 bytes 或 None"""
    if not seek(h, offset):
        return None
    buf = ctypes.create_string_buffer(size)
    got = wt.DWORD(0)
    ctypes.set_last_error(0)
    ok = ReadFile(h, buf, size, ctypes.byref(got), None)
    if not ok or got.value != size:
        return None
    return buf.raw[:got.value]


class Map:
    """块级进度表，支持 --resume"""

    def __init__(self, path, total_blocks):
        self.path = path
        self.state = bytearray(total_blocks)  # 0=未读 1=好 2=坏
        if os.path.exists(path):
            try:
                d = json.load(open(path, encoding="utf-8"))
                raw = d.get("state", {})
                for k, v in raw.items():
                    self.state[int(k)] = v
            except Exception:
                pass

    def save(self):
        done = {str(i): v for i, v in enumerate(self.state) if v}
        tmp = self.path + ".tmp"
        json.dump({"state": done}, open(tmp, "w", encoding="utf-8"))
        os.replace(tmp, self.path)

    def counts(self):
        a = self.state.count(1)
        b = self.state.count(2)
        return a, b


def bulk_pass(h, iso, m, base, total_bytes, chunk, log=print):
    t0 = time.perf_counter()
    read_bytes = 0
    n_blocks = len(m.state)
    for i in range(n_blocks):
        if m.state[i]:            # resume: 已处理
            continue
        off = base + i * chunk
        size = min(chunk, total_bytes - i * chunk)
        if size <= 0:
            break
        data = read_at(h, off, size)
        iso.seek(i * chunk)
        if data is None:
            m.state[i] = 2        # 记坏，稍后重试
            iso.write(b"\0" * size)
        else:
            m.state[i] = 1
            iso.write(data)
            read_bytes += size
        if i % 32 == 0:
            el = time.perf_counter() - t0
            done_mb = (i + 1) * chunk / 1048576
            speed = done_mb / el if el else 0
            log(f"  bulk {done_mb:7.1f} MB  {speed:5.2f} MB/s  bad_blocks={m.state.count(2)}")
        if i % 256 == 0:
            m.save()
    m.save()
    return time.perf_counter() - t0


def retry_block(h, offset, size, retries=6):
    """对一个坏块做逐级缩小的重试，成功返回 (bytes, 用了多大块)"""
    for bs in (size // 4, 64 * 1024, 32 * 1024, 8 * 1024, SECTOR):
        if bs < SECTOR:
            continue
        for _ in range(retries):
            for direction in (1, -1):
                offs = [offset + k * bs for k in range(0, size // bs)]
                if direction < 0:
                    offs = offs[::-1]
                parts = {}
                ok_all = True
                for o in offs:
                    d1 = read_at(h, o, bs)
                    d2 = read_at(h, o, bs)   # 读两次比对，破缓存
                    if d1 is None or d1 != d2:
                        ok_all = False
                        break
                    parts[o] = d1
                if ok_all and len(parts) * bs >= size - bs:
                    out = b"".join(parts[o] for o in sorted(parts))
                    return out[:size], bs
    return None, None


def retry_pass(h, iso, m, base, total_bytes, chunk, retries, log=print):
    bad = [i for i, v in enumerate(m.state) if v == 2]
    if not bad:
        return 0, 0, []
    log(f"  需要重试的块: {len(bad)} (约 {len(bad)*chunk/1048576:.0f} MB)")
    recovered = 0
    for n, i in enumerate(bad, 1):
        off = base + i * chunk
        size = min(chunk, total_bytes - i * chunk)
        data, used = retry_block(h, off, size, retries)
        if data:
            iso.seek(i * chunk)
            iso.write(data)
            m.state[i] = 1
            recovered += size
            log(f"  救回块 #{i}（{size//1024} KB，用 {used//1024} KB 粒度）")
        else:
            if n % 4 == 0:
                log(f"  重试进度 {n}/{len(bad)}，救回 {recovered/1048576:.1f} MB")
    m.save()
    return recovered, len(bad), bad


def merge_ranges(state, base, chunk):
    ranges = []
    cur = None
    for i, v in enumerate(state):
        if v == 2:
            lba = (base + i * chunk) // SECTOR
            if cur and lba == cur[1]:
                cur[1] = lba + chunk // SECTOR
            else:
                if cur:
                    ranges.append(cur)
                cur = [lba, lba + chunk // SECTOR]
    if cur:
        ranges.append(cur)
    return ranges


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("drive")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--start-mb", type=float, default=0)
    ap.add_argument("--length-mb", type=float, default=0, help="0 = 到盘尾")
    ap.add_argument("--chunk-kb", type=int, default=1024)
    ap.add_argument("--retries", type=int, default=6)
    ap.add_argument("--resume", action="store_true")
    a = ap.parse_args()

    h, path = open_drive(a.drive)
    cap = read_capacity10(h)
    if not cap:
        print("拿不到容量，退出")
        return 1
    last_lba, blk = cap
    total_sectors = last_lba + 1
    disc_bytes = total_sectors * blk
    print(f"设备 {path}  容量 {disc_bytes/1073741824:.2f} GB ({total_sectors} 扇区 x {blk})")

    base = int(a.start_mb * 1048576)
    length = int(a.length_mb * 1048576) or (disc_bytes - base)
    length = min(length, disc_bytes - base)
    chunk = a.chunk_kb * 1024
    n_blocks = (length + chunk - 1) // chunk
    print(f"范围: {base/1048576:.0f} MB 起，{length/1048576:.0f} MB，块 {a.chunk_kb} KB，共 {n_blocks} 块")

    m = Map(a.out + ".map", n_blocks) if a.resume else Map(a.out + ".new", n_blocks)
    if not a.resume and os.path.exists(a.out + ".new"):
        os.remove(a.out + ".new")
    m.path = a.out + ".map"

    mode = "r+b" if (a.resume and os.path.exists(a.out)) else "w+b"
    iso = open(a.out, mode)
    if mode == "w+b":
        iso.truncate(length)

    t_start = time.perf_counter()
    print("\n[pass 1] 顺序大块读")
    t_bulk = bulk_pass(h, iso, m, base, length, chunk)
    good, bad = m.counts()
    print(f"  pass1 完成：好块 {good}，坏块 {bad}，用时 {t_bulk/60:.1f} 分钟")

    print("\n[pass 2] 缩小粒度重试坏块")
    recovered, n_bad, bad_idx = retry_pass(h, iso, m, base, length, chunk, a.retries)
    good, bad = m.counts()
    elapsed = time.perf_counter() - t_start

    ranges = merge_ranges(m.state, base, chunk)
    badmap = dict(drive=path, total_bytes=disc_bytes, range_start_mb=a.start_mb,
                  range_length_mb=round(length/1048576, 1),
                  bad_sectors=ranges, bad_mb=round(bad * chunk / 1048576, 1),
                  recovered_mb=round(recovered / 1048576, 1),
                  chunks_retried=n_bad, iso=a.out,
                  generated=time.strftime("%Y-%m-%dT%H:%M:%S"))
    json.dump(badmap, open(a.out + ".badmap.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    iso.close()
    kernel32.CloseHandle(h)

    print(f"\n[pass 3] 汇总")
    print(f"  用时 {elapsed/60:.1f} 分钟")
    print(f"  好块 {good} / 坏块 {bad} / 重试救回 {recovered/1048576:.1f} MB")
    print(f"  坏区 {len(ranges)} 段，合计 {badmap['bad_mb']} MB")
    if ranges[:8]:
        print("  前几段坏区（LBA）:")
        for r in ranges[:8]:
            print(f"    {r[0]:>9} - {r[1]:>9}")
    print(f"  输出: {a.out}")
    print(f"  坏块图: {a.out}.badmap.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
