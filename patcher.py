#!/usr/bin/env python3
"""
Project Earth: Patcher — desktop edition
Patches Minecraft Earth APK to use community servers.
"""

import argparse
import os
import re
import shutil
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

import requests

PATCHES_URL = "https://github.com/Project-Earth-Team/Patches/archive/main.zip"
DEFAULT_SERVER = "https://p.projectearth.dev"
SERVER_MAX = 27
SUNSET_OFF = 0x22A6DC8
SUNSET_VAL = 0x540005CB
ADDR_OFF = 0x0514D05D


def bail(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def need(cmd):
    if shutil.which(cmd) is None:
        bail(f"'{cmd}' not found — install it first")


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


def decompile(apk: Path, out: Path):
    print("decompiling apk...")
    if out.exists():
        shutil.rmtree(out)
    subprocess.run(["apktool", "d", "-f", "-o", str(out), str(apk)], check=True)


def normalize_line_endings(root: Path, rel_path: str):
    """Rewrite a file with LF line endings (mirrors AndroidUtils.normalizeFile)."""
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
        bail(f"{so} not found — wrong apk?")

    if not re.match(r"^https?://", server):
        server = "https://" + server
    server = server.rstrip("/")
    if len(server) > SERVER_MAX:
        bail(f"server too long ({len(server)} > {SERVER_MAX})")
    padded = server.ljust(SERVER_MAX, "\0").encode("ascii")

    data = so.read_bytes()
    # Server address
    data = data[:ADDR_OFF] + padded + data[ADDR_OFF + len(padded) :]
    # Sunset check
    data = data[:SUNSET_OFF] + struct.pack("<I", SUNSET_VAL) + data[SUNSET_OFF + 4 :]
    so.write_bytes(data)


def apply_patches(out_dir: Path, patch_dir: Path):
    print("applying patches...")
    patches = sorted(patch_dir.glob("*.patch"))
    if not patches:
        return
    # Init a temporary git repo so git apply works
    subprocess.run(["git", "init"], cwd=out_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=p", "-c", "user.email=p@p", "config", "user.email", "p@p"],
        cwd=out_dir, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-c", "user.name=p", "-c", "user.email=p@p", "config", "user.name", "p"],
        cwd=out_dir, check=True, capture_output=True,
    )

    for pf in patches:
        # Normalize line endings of target files before applying (matches original behaviour)
        p = PatchFile(pf)
        for fpath in p.target_files():
            normalize_line_endings(out_dir, fpath)
        subprocess.run(
            ["git", "apply", "--whitespace=nowarn", str(pf)],
            cwd=out_dir, check=True,
        )
        print(f"  {pf.name}")


class PatchFile:
    """Minimal .patch parser to extract target file paths."""
    def __init__(self, path: Path):
        self.path = path
        self._targets = []
        with open(path) as f:
            for line in f:
                line = line.rstrip("\n\r")
                m = re.match(r"^--- (?:a/)?(.+?)(?:\t.*)?$", line)
                if m:
                    self._targets.append(m.group(1))

    def target_files(self):
        return self._targets


def recompile(out_dir: Path, output: Path):
    print("recompiling...")
    if output.exists():
        output.unlink()
    subprocess.run(["apktool", "b", "-o", str(output), str(out_dir)], check=True)


def sign_apk(input_apk: Path, ks: Path, output: Path):
    print("signing...")
    if output.exists():
        output.unlink()
    aligned = input_apk.with_suffix(".aligned.apk")
    subprocess.run(["zipalign", "-p", "-f", "4", str(input_apk), str(aligned)], check=True)
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

    need("apktool")
    need("git")
    need("zipalign")
    need("apksigner")

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
