#!/usr/bin/env python3
"""
Project Earth: Patcher — desktop edition
Patches Minecraft Earth APK to use community servers.
"""

import argparse
import re
import shutil
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

import requests
from patch import fromstring

PATCHES_URL = "https://github.com/Project-Earth-Team/Patches/archive/main.zip"
DEFAULT_SERVER = "https://p.projectearth.dev"
SERVER_MAX = 27
SUNSET_OFF = 0x22A6DC8
SUNSET_VAL = 0x540005CB
ADDR_OFF = 0x0514D05D

KNOWN_GOOD_CODES = {2020121703}


def bail(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def need(cmd):
    if shutil.which(cmd) is None:
        return False
    return True


def fmt_size(n):
    for unit in ("", "K", "M"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}G"


def validate_apk(apk: Path):
    print("validating apk...")
    size = apk.stat().st_size
    print(f"  size: {fmt_size(size)}")

    with zipfile.ZipFile(apk) as zf:
        names = set(zf.namelist())

    if "lib/arm64-v8a/libgenoa.so" not in names:
        bail("APK doesn't contain lib/arm64-v8a/libgenoa.so — not Minecraft Earth")

    # Try to extract version code from binary AndroidManifest
    # Search for known version codes as raw 4-byte LE ints anywhere in the file
    try:
        with zipfile.ZipFile(apk) as zf:
            raw = zf.read("AndroidManifest.xml")
        known_le = {struct.pack("<I", v) for v in KNOWN_GOOD_CODES}
        found = None
        for i in range(len(raw) - 3):
            chunk = raw[i : i + 4]
            if chunk in known_le:
                v = struct.unpack("<I", chunk)[0]
                found = v
                break
            # Also try as big-endian
            if chunk[::-1] in known_le:
                v = struct.unpack(">I", chunk)[0]
                found = v
                break
        if found:
            print(f"  version code: {found}")
            if found not in KNOWN_GOOD_CODES:
                print(
                    f"  warning: version code {found} not in known"
                    f" compatible set {sorted(KNOWN_GOOD_CODES)}"
                )
        else:
            print("  warning: could not determine version code from AndroidManifest")
    except KeyError:
        print("  warning: AndroidManifest.xml not found in APK")


def download_patches(patch_dir: Path):
    print("downloading patches...")
    patch_dir.mkdir(parents=True, exist_ok=True)
    for child in patch_dir.iterdir():
        child.unlink()

    resp = requests.get(PATCHES_URL, stream=True)
    resp.raise_for_status()
    tmp = patch_dir / "patches.zip"
    with open(tmp, "wb") as f:
        for chunk in resp.iter_content(8192):
            f.write(chunk)
    with zipfile.ZipFile(tmp) as zf:
        for entry in zf.infolist():
            if not entry.filename.endswith(".patch"):
                continue
            name = Path(entry.filename).name
            zf.extract(entry, patch_dir)
            extracted = patch_dir / entry.filename
            target = patch_dir / name
            if extracted != target:
                shutil.move(str(extracted), str(target))
                extracted.parent.rmdir()
    tmp.unlink()

    count = len(list(patch_dir.glob("*.patch")))
    print(f"  {count} patch files downloaded")


def decompile(apk: Path, out: Path):
    print("decompiling apk...")
    if out.exists():
        shutil.rmtree(out)
    subprocess.run(["apktool", "d", "-f", "-o", str(out), str(apk)], check=True)


def normalize_line_endings(root: Path, rel_path: str):
    f = root / rel_path
    if not f.exists():
        return
    data = f.read_bytes()
    data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    f.write_bytes(data)


def patch_binary(out_dir: Path, server: str):
    print("patching libgenoa.so...")
    so = out_dir / "lib" / "arm64-v8a" / "libgenoa.so"
    if not so.exists():
        bail(f"{so} not found — decompile may have failed")

    if not re.match(r"^https?://", server):
        server = "https://" + server
    server = server.rstrip("/")
    if len(server) > SERVER_MAX:
        bail(f"server too long ({len(server)} > {SERVER_MAX})")
    padded = server.ljust(SERVER_MAX, "\0").encode("ascii")

    data = so.read_bytes()
    data = data[:ADDR_OFF] + padded + data[ADDR_OFF + len(padded) :]
    data = (
        data[:SUNSET_OFF]
        + struct.pack("<I", SUNSET_VAL)
        + data[SUNSET_OFF + 4 :]
    )
    so.write_bytes(data)


def apply_patches(out_dir: Path, patch_dir: Path):
    print("applying patches...")
    patches = sorted(patch_dir.glob("*.patch"))
    if not patches:
        print("  no patches to apply")
        return

    for pf in patches:
        content = pf.read_bytes()
        # Normalize CRLF -> LF in the patch content itself
        content = content.replace(b"\r\n", b"\n").replace(b"\r", b"\n")

        ps = fromstring(content)
        if ps is None:
            bail(f"failed to parse patch: {pf.name}")

        # Normalize line endings of target files before patching
        for item in ps.items:
            # item.target is like b'b/smali/foo.smali' — strip b/ prefix
            target_path = item.target.decode()
            target_path = re.sub(r"^[ab]/", "", target_path)
            normalize_line_endings(out_dir, target_path)

        if not ps.apply(root=str(out_dir), strip=1):
            bail(f"failed to apply patch: {pf.name}")
        print(f"  {pf.name}")


def recompile(out_dir: Path, output: Path):
    print("recompiling...")
    if output.exists():
        output.unlink()
    subprocess.run(["apktool", "b", "-o", str(output), str(out_dir)], check=True)
    sz = output.stat().st_size
    print(f"  built: {fmt_size(sz)}")


def sign_apk(input_apk: Path, ks: Path, output: Path):
    print("signing...")
    if output.exists():
        output.unlink()

    if need("apksigner") and need("zipalign"):
        print("  using apksigner + zipalign")
        aligned = input_apk.with_suffix(".aligned.apk")
        subprocess.run(
            ["zipalign", "-p", "-f", "4", str(input_apk), str(aligned)],
            check=True,
        )
        subprocess.run(
            [
                "apksigner", "sign",
                "--ks", str(ks),
                "--ks-key-alias", "earth_test",
                "--ks-pass", "pass:earth_test",
                "--key-pass", "pass:earth_test",
                "--v1-signing-enabled", "true",
                "--v2-signing-enabled", "true",
                "--out", str(output),
                str(aligned),
            ],
            check=True,
        )
        aligned.unlink()
    elif need("jarsigner"):
        print("  using jarsigner (v1 signing only — no zipalign/aligned)")
        subprocess.run(
            [
                "jarsigner",
                "-keystore", str(ks),
                "-storepass", "earth_test",
                "-keypass", "earth_test",
                "-signedjar", str(output),
                str(input_apk),
                "earth_test",
            ],
            check=True,
        )
    else:
        bail(
            "no signing tool found — install apksigner+zipalign (Android SDK)"
            " or jarsigner (JDK)"
        )
    sz = output.stat().st_size
    print(f"  signed: {fmt_size(sz)}")


def main():
    ap = argparse.ArgumentParser(description="Patch Minecraft Earth APK for community servers")
    ap.add_argument("apk", type=Path, help="path to Minecraft Earth APK")
    ap.add_argument("--server", default=DEFAULT_SERVER, help=f"server url (default: {DEFAULT_SERVER})")
    ap.add_argument("--out", "-o", type=Path, default=Path.cwd(), help="output directory")
    ap.add_argument("--keystore", type=Path, help="keystore path (default: bundled earth_test.jks)")
    ap.add_argument("--skip-download", action="store_true", help="use cached patches")
    args = ap.parse_args()

    apk = args.apk.resolve()
    if not apk.exists():
        bail(f"apk not found: {apk}")

    base = args.out.resolve()
    base.mkdir(parents=True, exist_ok=True)

    script_dir = Path(__file__).parent
    ks = args.keystore or (script_dir / "resources" / "earth_test.jks")
    if not ks.exists():
        bail(f"keystore not found: {ks}")

    work = base / "work"
    patch_dir = work / "patches"
    deco_dir = work / "com.mojang.minecraftearth"
    unsigned = work / "dev.projectearth.prod.unsigned.apk"
    signed = base / "dev.projectearth.prod.apk"

    need("apktool") or bail("apktool not found — install it first")

    validate_apk(apk)

    if not args.skip_download:
        download_patches(patch_dir)

    decompile(apk, deco_dir)
    patch_binary(deco_dir, args.server)
    apply_patches(deco_dir, patch_dir)
    recompile(deco_dir, unsigned)
    sign_apk(unsigned, ks, signed)
    unsigned.unlink(missing_ok=True)
    shutil.rmtree(work)

    print(f"\ndone — patched apk: {signed}")


if __name__ == "__main__":
    main()
