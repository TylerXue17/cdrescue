# cdrescue

**Recover data from scratched, failing or rotting optical discs — and actually watch what comes out.**

Most tools stop at "I made an ISO with holes in it". `cdrescue` aims one step further:
map *which sectors* are bad, retry only those, and hand the result to a player/encoder that
knows how to skip damage instead of freezing.

> Status: **feasibility stage.** The probe tool below already works and produces the
> hardware data the design depends on. The rescue engine is next.

---

## The problem this exists for

A 2024-made DVD-R of a 1997 cartoon series plays fine on the first 3 GB and becomes
unreadable past ~3.2 GB. Symptoms:

- `dd` returns **0 bytes in 28 seconds** on the outer region
- `ffmpeg` stops mid-episode with `Invalid data found when processing input`
- Players either freeze, or silently skip and you never learn what you lost
- `ddrescue` exists but is awkward on Windows and does not touch the drive's own
  error-recovery behaviour

## Why a new tool (what is actually missing)

| Tool | Reads at slow speed | Tunes drive retry policy | Per-sector bad map | Player-side skip |
|---|---|---|---|---|
| `ddrescue` | ✗ | ✗ | ✓ | ✗ |
| `dvdisaster` | ✗ | ✗ | ✓ | ✗ |
| Exact Audio Copy | ✓ (CD audio only) | ✓ (SPTI) | ✓ (audio) | ✗ |
| IsoBuster (paid) | ✓ | partial | ✓ | ✗ |
| VLC / mpv | ✗ | ✗ | ✗ | ✓ (uncontrolled) |

The gap: **Windows-native, sector-level rescue that also talks to the drive's error-recovery
mode page**, plus a bad-sector map the player can consume.

---

## `tools/probe_drive.py` — hardware capability probe

Before building a rescue engine you need to know what your drive will actually let you do.
This script answers that with measurements, not guesses.

```
python tools/probe_drive.py F: --points 10 --sectors 32 --json out.json
```

It measures raw sector reads via **SPTI** (bypassing the Windows cache), then tries to apply
slow-speed + aggressive-retry settings and measures the same points again.

### Findings so far (1 drive tested, 2026-11, Windows 10 + ffmpeg-era slim DVD writer)

| Capability | Result | Evidence |
|---|---|---|
| `READ(10)` via SPTI | **rejected** with `ERROR_SEM_TIMEOUT` (121) | every LBA, 1–16 sectors |
| `READ(12)` via SPTI | **works** | 8/8 reads OK, 32–64 KB per command |
| `IOCTL_CDROM_GET_DRIVE_GEOMETRY` | returns `ERROR_NOT_SUPPORTED` (50) | must use `READ CAPACITY(10)` |
| `IOCTL_CDROM_SET_SPEED` (1x) | **rejected**, `err=1785`, all request types | no speed control on this drive |
| `MODE SENSE(10)` page 0x01 | **works**, retry count reads back as `0x80` (128) | default is already aggressive |
| `MODE SELECT(10)` page 0x01 | **times out** | drive does not accept the write |
| `MODE SELECT(6)` page 0x01 | `CHECK CONDITION` (status 2), page unchanged | rejected |
| SPTI throughput | ~0.34 MB/s @ 64 KB reads | per-command overhead dominates |
| Filesystem read throughput | ~5.4 MB/s (healthy region) | 16× faster than SPTI |

**Takeaway:** on this hardware the firmware-side "super error correction" knobs
(speed + retry policy) are **not reachable**. That does not kill the project — it means the
value has to come from the software side: a hybrid reader (fast bulk pass + per-sector
retries only on the damaged range), an honest bad-sector map, resumable multi-pass
scanning, and player-side damage skipping.

The probe doubles as a **community data collection tool**: report your drive in the issues
and this table grows into a "which drives honour which knobs" reference that does not
exist anywhere else.

---

## Design (next)

```
pass 0  capacity + media info, estimate damage layout
pass 1  bulk read via OS/filesystem at full speed, record failed 64 KB blocks
pass 2  failed blocks only: SPTI per-sector reads, N retries, alternating direction,
        read-twice-and-compare to defeat any cache
pass 3  trim + retry shrunken ranges, then give up on truly dead sectors
out     disc.iso + badmap.json (LBA ranges) + report.html
        optional: --recover-video → ffmpeg with -err_detect ignore_err, timestamp repair
```

None of this writes to the disc. Ever.

## Licence

MIT — free for any use, including commercial. See `LICENSE`.

## Contributing hardware data

Run the probe on your drive, paste the JSON in a GitHub issue titled
`probe: <drive model>`. Even "nothing works on my drive" is valuable data.
