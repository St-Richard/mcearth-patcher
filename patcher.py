#!/usr/bin/env python3
"""
Project Earth: Patcher — desktop edition
Patches Minecraft Earth APK to use community servers.
"""

import argparse
import base64
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import zlib
import zipfile
from pathlib import Path

import requests
from patch import fromstring

PATCHES_URL = "https://github.com/Project-Earth-Team/Patches/archive/main.zip"
APKTOOL_URL = "https://github.com/iBotPeaches/Apktool/releases/download/v2.11.0/apktool_2.11.0.jar"
DEFAULT_SERVER = "https://p.projectearth.dev"
SERVER_MAX = 27
SUNSET_OFF = 0x22A6DC8
SUNSET_VAL = 0x540005CB
ADDR_OFF = 0x0514D05D
JDK_VERSION = 21

KNOWN_GOOD_CODES = {2020121703}


def bail(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def fmt_size(n):
    for unit in ("", "K", "M"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}G"


def which(cmd):
    return shutil.which(cmd)


def _download(url, dest):
    resp = requests.get(url, stream=True)
    resp.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(8192):
            f.write(chunk)


def _detect_os_arch():
    system = platform.system().lower()
    machine = platform.machine().lower()
    os_map = {"linux": "linux", "darwin": "mac", "windows": "windows"}
    arch_map = {"x86_64": "x64", "amd64": "x64", "aarch64": "aarch64", "arm64": "aarch64"}
    os_name = os_map.get(system)
    arch_name = arch_map.get(machine)
    if not os_name:
        bail(f"unsupported OS: {system}")
    if not arch_name:
        bail(f"unsupported architecture: {machine}")
    return os_name, arch_name


def _extract_archive(path, dest):
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            zf.extractall(dest)
    else:
        with tarfile.open(path) as tf:
            tf.extractall(dest)


def _find_jdk_home(parent):
    for entry in sorted(parent.iterdir()):
        if entry.is_dir() and entry.name.startswith("jdk-"):
            return entry
    return parent


def resolve_or_download_jdk(tools_dir, override=None):
    if override:
        d = Path(override)
        java = d / "bin/java"
        if sys.platform == "win32":
            java = java.with_suffix(".exe")
        if not java.exists():
            bail(f"jdk not found at: {override}")
        js = d / ("bin/jarsigner" + (".exe" if sys.platform == "win32" else ""))
        return java, js

    # Check system
    sys_java = which("java")
    if sys_java:
        java = Path(sys_java)
        js = java.with_name("jarsigner")
        if sys.platform == "win32":
            js = js.with_suffix(".exe")
        if js.exists():
            return java, js
        jh = os.environ.get("JAVA_HOME")
        if jh:
            js = Path(jh) / "bin" / ("jarsigner" + (".exe" if sys.platform == "win32" else ""))
            if js.exists():
                return java, js

    # Try bundled
    jdk_dir = tools_dir / "jdk"
    extracted_mark = jdk_dir / ".extracted"
    if extracted_mark.exists():
        jdk_home = _find_jdk_home(jdk_dir)
        java = jdk_home / ("bin/java" + (".exe" if sys.platform == "win32" else ""))
        js = jdk_home / ("bin/jarsigner" + (".exe" if sys.platform == "win32" else ""))
        if java.exists() and js.exists():
            return java, js

    # Download
    os_name, arch_name = _detect_os_arch()
    ext = "zip" if os_name == "windows" else "tar.gz"
    url = f"https://api.adoptium.net/v3/binary/latest/{JDK_VERSION}/ga/{os_name}/{arch_name}/jdk/hotspot/normal/eclipse"
    print(f"downloading JDK {JDK_VERSION} for {os_name}-{arch_name}...")
    jdk_dir.mkdir(parents=True, exist_ok=True)
    archive = jdk_dir / f"jdk.{ext}"
    _download(url, archive)
    print("extracting...")
    _extract_archive(archive, jdk_dir)
    archive.unlink()
    extracted_mark.touch()

    jdk_home = _find_jdk_home(jdk_dir)
    java = jdk_home / ("bin/java" + (".exe" if sys.platform == "win32" else ""))
    js = jdk_home / ("bin/jarsigner" + (".exe" if sys.platform == "win32" else ""))
    if not java.exists():
        bail(f"downloaded JDK broken — {java} not found")
    return java, js


def resolve_apktool(path_override):
    if path_override:
        p = Path(path_override)
        if p.exists():
            return p
        bail(f"apktool not found at: {p}")

    which_apt = which("apktool")
    if which_apt:
        return which_apt

    cached = Path(__file__).parent / "tools" / "apktool.jar"
    if cached.exists():
        return cached

    print("apktool not found — downloading...")
    cached.parent.mkdir(parents=True, exist_ok=True)
    _download(APKTOOL_URL, cached)
    cached.chmod(0o755)
    return cached


def run_apktool(apktool, args, java):
    apktool = Path(apktool)
    if apktool.suffix == ".jar":
        cmd = [str(java), "-jar", str(apktool)] + args
    else:
        cmd = [str(apktool)] + args
    subprocess.run(cmd, check=True)


def validate_apk(apk: Path):
    print("validating apk...")
    sz = apk.stat().st_size
    print(f"  size: {fmt_size(sz)}")
    with zipfile.ZipFile(apk) as zf:
        names = set(zf.namelist())
    if "lib/arm64-v8a/libgenoa.so" not in names:
        bail("APK doesn't contain lib/arm64-v8a/libgenoa.so — not Minecraft Earth")
    try:
        with zipfile.ZipFile(apk) as zf:
            raw = zf.read("AndroidManifest.xml")
        known_le = {struct.pack("<I", v) for v in KNOWN_GOOD_CODES}
        found = None
        for i in range(len(raw) - 3):
            chunk = raw[i : i + 4]
            if chunk in known_le:
                found = struct.unpack("<I", chunk)[0]
                break
            if chunk[::-1] in known_le:
                found = struct.unpack(">I", chunk)[0]
                break
        if found:
            print(f"  version code: {found}")
            if found not in KNOWN_GOOD_CODES:
                print(f"  warning: version code {found} not in known"
                      f" compatible set {sorted(KNOWN_GOOD_CODES)}")
        else:
            print("  warning: could not determine version code from AndroidManifest")
    except KeyError:
        print("  warning: AndroidManifest.xml not found in APK")


def download_patches(patch_dir: Path):
    print("downloading patches...")
    patch_dir.mkdir(parents=True, exist_ok=True)
    for child in patch_dir.iterdir():
        child.unlink()
    tmp = patch_dir / "patches.zip"
    _download(PATCHES_URL, tmp)
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
    print(f"  {len(list(patch_dir.glob('*.patch')))} patch files downloaded")


def decompile(apk: Path, out: Path, apktool, java):
    print("decompiling apk...")
    if out.exists():
        shutil.rmtree(out)
    run_apktool(apktool, ["d", "-f", "-o", str(out), str(apk)], java)


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
    data = data[:SUNSET_OFF] + struct.pack("<I", SUNSET_VAL) + data[SUNSET_OFF + 4 :]
    so.write_bytes(data)


def patch_manifest_package(out_dir: Path):
    """Apply package-name changes to decompiled AndroidManifest.xml directly."""
    mf = out_dir / "AndroidManifest.xml"
    if not mf.exists():
        return
    old = "com.mojang.minecraftearth"
    new = "dev.projectearth.prod"
    text = mf.read_text(encoding="utf-8")
    if old not in text:
        return
    text = text.replace(old, new)
    mf.write_text(text, encoding="utf-8")
    print("  [inline] package name in AndroidManifest.xml")


def _git_b85decode(data):
    """Decode git's base85 (ascii85) encoding (RFC 1924 variant)."""
    return base64.b85decode(data)


def _apply_one_binary_section(out_dir, target, mode, b85_lines):
    """Apply one literal binary section from a git binary patch."""
    target_path = out_dir / target

    if mode == "deleted":
        if target_path.exists():
            target_path.unlink()
        return True

    b85_chunks = []
    for line in b85_lines:
        line = line.strip()
        if not line:
            continue
        if len(line) < 1:
            continue
        count = ord(line[0]) - 32
        if count <= 0:
            continue
        b85_data = line[1:]
        if len(b85_data) != count:
            b85_data = b85_data[:count]
        b85_chunks.append(b85_data)

    if not b85_chunks:
        return True

    raw = "".join(b85_chunks)
    compressed = _git_b85decode(raw)
    try:
        content = zlib.decompress(compressed)
    except zlib.error:
        content = zlib.decompress(compressed, -zlib.MAX_WBITS)

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(content)
    if mode:
        target_path.chmod(int(mode, 8))
    return True


def _parse_git_binary_patch(out_dir: Path, pf: Path):
    """Apply a git binary patch that may contain multiple file sections."""
    text = pf.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    i = 0
    while i < len(lines):
        m = re.match(r"^diff --git a/(.+?) b/(.+?)$", lines[i])
        if not m:
            i += 1
            continue
        target = m.group(2)
        mode = None
        i += 1

        # Skip to the binary content section
        while i < len(lines) and not lines[i].startswith(("literal ", "delta ")):
            rm = re.match(r"^(new|deleted) file mode (\d+)$", lines[i])
            if rm:
                mode = rm.group(2) if rm.group(1) == "new" else "deleted"
            elif lines[i] == "GIT binary patch":
                pass
            i += 1

        if i >= len(lines):
            break

        if lines[i].startswith("literal "):
            binary_type = "literal"
        elif lines[i].startswith("delta "):
            binary_type = "delta"
        else:
            continue

        i += 1  # skip "literal N" line

        # Collect b85 lines until next section or end
        b85_lines = []
        while i < len(lines):
            line = lines[i]
            if line.startswith("diff --git") or line.startswith("literal ") or line.startswith("delta "):
                break
            b85_lines.append(line)
            i += 1

        if binary_type == "literal":
            if not _apply_one_binary_section(out_dir, target, mode, b85_lines):
                return False

    return True


def _patch_targets(pf):
    """Return list of target file paths from a patch (relative, no a/b prefix)."""
    content = pf.read_bytes()
    idx = content.find(b"diff --git")
    if idx >= 0:
        content = content[idx:]
    ps = fromstring(content)
    if not ps:
        return []
    targets = []
    for item in ps.items:
        t = item.target.decode()
        targets.append(re.sub(r"^[ab]/", "", t))
    return targets


def apply_patches(out_dir: Path, patch_dir: Path):
    print("applying patches...")
    patches = sorted(patch_dir.glob("*.patch"))
    if not patches:
        print("  no patches to apply")
        return

    for pf in patches:
        targets = _patch_targets(pf)

        # Handle AndroidManifest.xml changes inline (apktool version skew)
        if "AndroidManifest.xml" in targets:
            patch_manifest_package(out_dir)
            print(f"  {pf.name} (inline)")
            continue

        content = pf.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")

        # Normalise line endings of target files
        for t in targets:
            normalize_line_endings(out_dir, t)

        ps = fromstring(content)
        if not ps:
            # Not a text patch — try git binary patch format
            if b"GIT binary patch" in content:
                if _parse_git_binary_patch(out_dir, pf):
                    print(f"  {pf.name}")
                    continue
            bail(f"failed to parse patch: {pf.name}")

        if not ps.apply(root=str(out_dir), strip=1):
            bail(f"failed to apply patch: {pf.name}")
        print(f"  {pf.name}")


def recompile(out_dir: Path, output: Path, apktool, java):
    print("recompiling...")
    if output.exists():
        output.unlink()
    run_apktool(apktool, ["b", "-o", str(output), str(out_dir)], java)
    print(f"  built: {fmt_size(output.stat().st_size)}")


def sign_apk(input_apk: Path, ks: Path, output: Path, jarsigner):
    print("signing...")
    if output.exists():
        output.unlink()
    if not jarsigner.exists():
        bail(f"jarsigner not found at: {jarsigner}")
    subprocess.run([
        str(jarsigner),
        "-keystore", str(ks),
        "-storepass", "earth_test",
        "-keypass", "earth_test",
        "-signedjar", str(output),
        str(input_apk),
        "earth_test",
    ], check=True)
    print(f"  signed: {fmt_size(output.stat().st_size)}")


def main():
    ap = argparse.ArgumentParser(description="Patch Minecraft Earth APK for community servers")
    ap.add_argument("apk", type=Path, help="path to Minecraft Earth APK")
    ap.add_argument("--server", default=DEFAULT_SERVER,
                    help=f"server url (default: {DEFAULT_SERVER})")
    ap.add_argument("--out", "-o", type=Path, default=Path.cwd(),
                    help="output directory (default: cwd)")
    ap.add_argument("--keystore", type=Path,
                    help="keystore path (default: bundled earth_test.jks)")
    ap.add_argument("--apktool", type=Path,
                    help="path to apktool.jar or apktool executable")
    ap.add_argument("--jdk", type=Path,
                    help="path to JDK home (default: auto-download if needed)")
    ap.add_argument("--skip-download", action="store_true",
                    help="use cached patches")
    args = ap.parse_args()

    apk = args.apk.resolve()
    if not apk.exists():
        bail(f"apk not found: {apk}")

    base = args.out.resolve()
    base.mkdir(parents=True, exist_ok=True)

    script_dir = Path(__file__).parent
    tools_dir = script_dir / "tools"

    ks = args.keystore or (script_dir / "resources" / "earth_test.p12")
    if not ks.exists():
        bail(f"keystore not found: {ks}")

    apktool = resolve_apktool(args.apktool)
    java, jarsigner = resolve_or_download_jdk(tools_dir, args.jdk)

    work = base / "work"
    patch_dir = work / "patches"
    deco_dir = work / "com.mojang.minecraftearth"
    unsigned = work / "dev.projectearth.prod.unsigned.apk"
    signed = base / "dev.projectearth.prod.apk"

    validate_apk(apk)

    if not args.skip_download:
        download_patches(patch_dir)

    decompile(apk, deco_dir, apktool, java)
    patch_binary(deco_dir, args.server)
    apply_patches(deco_dir, patch_dir)
    recompile(deco_dir, unsigned, apktool, java)
    sign_apk(unsigned, ks, signed, jarsigner)
    unsigned.unlink(missing_ok=True)
    shutil.rmtree(work)

    print(f"\ndone — patched apk: {signed}")


if __name__ == "__main__":
    main()
