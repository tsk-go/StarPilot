#!/usr/bin/env python3
"""
External USB storage for openpilot logs -- run ON the comma device.

    python3 extstorage.py                    # inspect: USB disks, fs support, current realdata usage. No changes.
    python3 extstorage.py --format /dev/sdX  # format that USB disk as ext4 (asks you to confirm), then install+mount
    python3 extstorage.py --install          # drive already ext4 -> install boot mount unit + mount now
    python3 extstorage.py --migrate          # copy existing realdata onto the drive before switching (with --install)
    python3 extstorage.py --status           # what is mounted where, free space, unit state
    python3 extstorage.py --uninstall        # remove the unit and unmount (data on the drive stays)

Design:
  * The drive is formatted ext4 with label EXTDATA and mounted directly on /data/media/0/realdata,
    so loggerd/uploader/deleter need zero code changes. If the drive is missing at boot the mount
    simply doesn't happen and openpilot logs to internal storage as before (nofail).
  * A systemd unit (extdata.service) does the mount at boot, before openpilot starts. AGNOS' root fs
    is read-only, so the unit is written after a temporary remount rw; an AGNOS *update* wipes it --
    just re-run --install afterwards. The real mount script lives in /data/extstorage/ which persists.
  * Only disks whose sysfs path goes through a USB controller are ever considered. Internal UFS is never touched.
"""
import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import time

LABEL = "EXTDATA"
MOUNTPOINT = "/data/media/0/realdata"
STAGING = "/data/extstorage"
MOUNT_SH = f"{STAGING}/extmount.sh"
UNIT = "/etc/systemd/system/extdata.service"
UID = 1000  # comma user


def sh(cmd, sudo=False, timeout=60, check=False):
    if sudo and os.geteuid() != 0:
        cmd = "sudo -n " + cmd
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    o = (p.stdout + p.stderr).strip()
    if check and p.returncode != 0:
        sys.exit(f"FAILED ({p.returncode}): {cmd}\n{o}")
    return p.returncode, o


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return default


def mounts():
    res = {}
    for l in read("/proc/mounts").splitlines():
        parts = l.split()
        if len(parts) >= 3:
            res.setdefault(parts[0], []).append((parts[1], parts[2]))
    return res


def usb_disks():
    """[(dev, size_bytes, vendor, model, [partitions])] for whole disks behind a USB controller."""
    res = []
    for blk in sorted(glob.glob("/sys/block/sd*")):
        name = os.path.basename(blk)
        real = os.path.realpath(blk)
        if "/usb" not in real:
            continue  # internal UFS / eMMC -> never
        size = int(read(f"{blk}/size", "0")) * 512
        vendor = read(f"{blk}/device/vendor")
        model = read(f"{blk}/device/model")
        parts = sorted(os.path.basename(p) for p in glob.glob(f"{blk}/{name}[0-9]*"))
        res.append((f"/dev/{name}", size, vendor, model, [f"/dev/{p}" for p in parts]))
    return res


def blkid(dev):
    rc, o = sh(f"blkid -o export {dev}", sudo=True)
    return dict(l.split("=", 1) for l in o.splitlines() if "=" in l) if rc == 0 else {}


def fs_support():
    fss = set(l.split()[-1] for l in read("/proc/filesystems").splitlines() if l.strip())
    return {fs: (fs in fss) for fs in ("ext4", "vfat", "exfat", "ntfs", "f2fs")}


def human(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}PB"


def is_onroad():
    return read("/data/params/d/IsOnroad") == "1"


def find_extdata():
    rc, o = sh(f"blkid -L {LABEL}", sudo=True)
    return o.strip() if rc == 0 and o.strip().startswith("/dev/") else None


# ----------------------------------------------------------------------------- inspect / status
def inspect():
    print("== USB block devices ==")
    disks = usb_disks()
    if not disks:
        print("  none. Is the drive plugged into the hub? (usb-storage takes a few seconds; dmesg | tail)")
    m = mounts()
    for dev, size, vendor, model, parts in disks:
        print(f"  {dev}  {human(size)}  [{vendor} {model}]")
        for p in parts or [dev]:
            info = blkid(p)
            mnt = ", ".join(f"{a} ({b})" for a, b in m.get(p, [])) or "not mounted"
            print(f"      {p}: fs={info.get('TYPE', '-')} label={info.get('LABEL', '-')} uuid={info.get('UUID', '-')}  {mnt}")
    print()
    print("== kernel filesystem support ==")
    print("  " + "  ".join(f"{k}={'yes' if v else 'no'}" for k, v in fs_support().items()))
    print("  tools: " + "  ".join(f"{t}={'yes' if shutil.which(t) else 'no'}" for t in ("mkfs.ext4", "blkid", "rsync", "systemctl")))
    print()
    print("== current log storage ==")
    rc, o = sh(f"df -h {MOUNTPOINT}")
    print("  " + o.replace("\n", "\n  "))
    rc, o = sh(f"du -sh {MOUNTPOINT} 2>/dev/null")
    print(f"  realdata size: {o.split()[0] if o else '?'}")
    ext = find_extdata()
    print(f"  {LABEL} partition: {ext or 'none found'}")
    print(f"  onroad now: {'YES (do not format/mount while driving)' if is_onroad() else 'no'}")


def status():
    ext = find_extdata()
    print(f"{LABEL} partition : {ext or 'not present'}")
    m = mounts()
    src = [d for d, lst in m.items() if any(mp == MOUNTPOINT for mp, _ in lst)]
    print(f"{MOUNTPOINT} : {'mounted from ' + src[0] if src else 'internal storage (nothing mounted)'}")
    rc, o = sh(f"df -h {MOUNTPOINT}")
    print("  " + o.replace("\n", "\n  "))
    rc, o = sh("systemctl is-enabled extdata.service 2>&1; systemctl is-active extdata.service 2>&1")
    print(f"extdata.service : {o.replace(chr(10), ' / ')}")
    print(f"mount script    : {'present' if os.path.exists(MOUNT_SH) else 'missing'} ({MOUNT_SH})")


# ----------------------------------------------------------------------------- format
def do_format(dev):
    disks = {d: (size, vendor, model, parts) for d, size, vendor, model, parts in usb_disks()}
    if dev not in disks:
        sys.exit(f"REFUSING: {dev} is not a USB-attached whole disk. Candidates: {list(disks) or 'none'}")
    size, vendor, model, parts = disks[dev]
    m = mounts()
    for p in [dev] + parts:
        for mp, _ in m.get(p, []):
            if mp in ("/", "/data", "/system", "/boot", "/persist", "/cache") or mp.startswith("/data/") and mp != MOUNTPOINT:
                sys.exit(f"REFUSING: {p} is mounted at {mp}")
    if is_onroad():
        sys.exit("REFUSING: device is onroad. Park, go offroad, retry.")
    if not shutil.which("mkfs.ext4"):
        sys.exit("mkfs.ext4 not found on this device (e2fsprogs missing) -- tell me, we'll format it another way")
    print(f"About to ERASE {dev}  {human(size)}  [{vendor} {model}]  partitions={parts or 'none'}")
    print(f"Everything on it will be gone. It will become one ext4 partition labeled {LABEL}.")
    ans = input(f"Type the device name ({dev}) to confirm: ").strip()
    if ans != dev:
        sys.exit("aborted")
    # unmount anything on it
    for p in [dev] + parts:
        for mp, _ in m.get(p, []):
            sh(f"umount {mp}", sudo=True)
    print("partitioning ...")
    # single GPT partition spanning the disk
    if shutil.which("sgdisk"):
        sh(f"sgdisk --zap-all {dev}", sudo=True, check=True)
        sh(f"sgdisk -n 1:0:0 -t 1:8300 -c 1:{LABEL} {dev}", sudo=True, check=True)
    elif shutil.which("parted"):
        sh(f"parted -s {dev} mklabel gpt mkpart {LABEL} ext4 1MiB 100%", sudo=True, check=True)
    else:
        sh(f"sh -c 'printf \"o\\nn\\np\\n1\\n\\n\\nw\\n\" | fdisk {dev}'", sudo=True, check=True)
    sh("partprobe " + dev, sudo=True)
    time.sleep(2)
    part = f"{dev}1" if not dev[-1].isdigit() else f"{dev}p1"
    for _ in range(10):
        if os.path.exists(part):
            break
        time.sleep(1)
    if not os.path.exists(part):
        sys.exit(f"partition {part} did not appear; run: ls /dev/sd*")
    print(f"formatting {part} as ext4 (this can take a minute on a big stick) ...")
    # -E root_owner: mount root owned by comma user; lazy init off so first boot isn't slow; ^64bit irrelevant on 4.9
    sh(f"mkfs.ext4 -F -L {LABEL} -m 0 -E root_owner={UID}:{UID},lazy_itable_init=0,lazy_journal_init=0 {part}",
       sudo=True, timeout=600, check=True)
    print("done.")
    return part


# ----------------------------------------------------------------------------- install
MOUNT_SCRIPT = f"""#!/bin/bash
# extmount.sh -- mount the {LABEL} USB partition on {MOUNTPOINT}. Installed by extstorage.py.
LABEL={LABEL}
MP={MOUNTPOINT}
for i in $(seq 1 30); do
  DEV=$(blkid -L "$LABEL" 2>/dev/null)
  [ -n "$DEV" ] && break
  sleep 1
done
if [ -z "$DEV" ]; then
  echo "extmount: no $LABEL partition after 30s, using internal storage"
  exit 0
fi
mkdir -p "$MP"
if mountpoint -q "$MP"; then
  echo "extmount: $MP already mounted"; exit 0
fi
# fix a dirty journal from a power cut before mounting
fsck.ext4 -p "$DEV" >/dev/null 2>&1 || true
if mount -t ext4 -o noatime,nodiratime,errors=remount-ro "$DEV" "$MP"; then
  chown {UID}:{UID} "$MP"
  echo "extmount: mounted $DEV on $MP"
else
  echo "extmount: mount failed, using internal storage"
fi
exit 0
"""

UNIT_TEXT = f"""[Unit]
Description=Mount external USB storage for openpilot logs
DefaultDependencies=no
After=local-fs.target systemd-udevd.service
Before=comma.service
ConditionPathExists={MOUNT_SH}

[Service]
Type=oneshot
ExecStart=/bin/bash {MOUNT_SH}
RemainAfterExit=yes
TimeoutStartSec=60

[Install]
WantedBy=multi-user.target
"""


def migrate(part):
    """Copy existing realdata onto the new partition via a temp mount."""
    tmp = "/data/extstorage/.migrate"
    os.makedirs(tmp, exist_ok=True)
    sh(f"mount -t ext4 {part} {tmp}", sudo=True, check=True)
    try:
        rc, o = sh(f"du -sh {MOUNTPOINT}")
        print(f"copying {o.split()[0] if o else '?'} of existing segments to the drive ...")
        if shutil.which("rsync"):
            sh(f"rsync -a --info=progress2 {MOUNTPOINT}/ {tmp}/", sudo=True, timeout=7200, check=True)
        else:
            sh(f"cp -a {MOUNTPOINT}/. {tmp}/", sudo=True, timeout=7200, check=True)
        sh(f"chown -R {UID}:{UID} {tmp}", sudo=True)
        print("copy done. (internal copy left in place; it becomes hidden under the mount and can be cleaned later)")
    finally:
        sh(f"umount {tmp}", sudo=True)
        os.rmdir(tmp)


def install(do_migrate=False):
    part = find_extdata()
    if not part:
        sys.exit(f"no partition labeled {LABEL} found. Run with --format /dev/sdX first (see: python3 {sys.argv[0]})")
    if is_onroad():
        sys.exit("REFUSING: device is onroad. Park, go offroad, retry.")
    if do_migrate:
        migrate(part)
    os.makedirs(STAGING, exist_ok=True)
    with open(MOUNT_SH, "w") as f:
        f.write(MOUNT_SCRIPT)
    os.chmod(MOUNT_SH, 0o755)
    print(f"wrote {MOUNT_SH}")

    # systemd unit on the read-only root
    rc, o = sh("mount -o remount,rw /", sudo=True)
    if rc != 0:
        print(f"could not remount / rw ({o}); skipping systemd unit.")
        print(f"  fallback: add this line near the top of your launch script (before openpilot starts):")
        print(f"      sudo /bin/bash {MOUNT_SH}")
    else:
        try:
            sh(f"sh -c 'cat > {UNIT}' <<'EOF'\n{UNIT_TEXT}EOF", sudo=True, check=True)
            sh("systemctl daemon-reload", sudo=True, check=True)
            sh("systemctl enable extdata.service", sudo=True, check=True)
            print(f"installed + enabled {UNIT}  (an AGNOS update removes it: re-run --install)")
        finally:
            sh("sync; mount -o remount,ro /", sudo=True)

    # mount now
    rc, o = sh(f"bash {MOUNT_SH}", sudo=True, timeout=120)
    print(o)
    status()


def uninstall():
    if is_onroad():
        sys.exit("REFUSING: device is onroad.")
    sh("systemctl disable extdata.service", sudo=True)
    rc, o = sh("mount -o remount,rw /", sudo=True)
    if rc == 0:
        sh(f"rm -f {UNIT}", sudo=True)
        sh("systemctl daemon-reload", sudo=True)
        sh("sync; mount -o remount,ro /", sudo=True)
    if any(mp == MOUNTPOINT for lst in mounts().values() for mp, _ in lst):
        rc, o = sh(f"umount {MOUNTPOINT}", sudo=True)
        print(f"umount: {'ok' if rc == 0 else o + '  (loggerd still writing? go offroad / reboot)'}")
    if os.path.exists(MOUNT_SH):
        os.remove(MOUNT_SH)
    print("uninstalled. Data on the drive is untouched; openpilot logs to internal storage again.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--format", metavar="/dev/sdX", help="erase this USB disk and make it the log drive")
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--migrate", action="store_true", help="copy current realdata to the drive first")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--live-mount", action="store_true", help="(not recommended) really mount the stick as openpilot's live log folder")
    a = ap.parse_args()
    # Recording straight onto the stick is NOT safe: when a stick stalls or drops off the hub, loggerd gets I/O errors
    # and crashes -> "Process Not Running" / "Communication Issue" take-overs (seen 2026-09-23). Use logarchive.py:
    # openpilot records internally, finished drives are copied to the stick while parked.
    if a.format:
        do_format(a.format)
        if a.live_mount:
            install(a.migrate)
        else:
            print("\nformatted as EXTDATA. It is NOT mounted as the live log folder (unsafe); logarchive.py copies\n"
                  "finished drives to it while parked. Check with: python3 /data/starview/logarchive.py --status")
    elif a.install:
        if not a.live_mount:
            sys.exit("--install mounts the stick as openpilot's LIVE log folder, which caused take-over alerts when the\n"
                     "stick stalled. Use /data/starview/logarchive.py instead (copies while parked).\n"
                     "If you really want the old behaviour: --install --live-mount")
        install(a.migrate)
    elif a.status:
        status()
    elif a.uninstall:
        uninstall()
    else:
        inspect()


if __name__ == "__main__":
    main()
