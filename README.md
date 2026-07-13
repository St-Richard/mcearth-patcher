# Project Earth: Patcher

Patches Minecraft Earth APK to redirect it to community-run servers.

## Prerequisites

- **Python 3.10+** and pip

Everything else (JDK 21, apktool) is auto-downloaded on first run.

## Usage

```bash
# One-time setup
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# Patch an APK
venv/bin/python patcher.py /path/to/MinecraftEarth.apk
```

The patched APK (`dev.projectearth.prod.apk`) is written to the current directory.

### Options

| Flag | Description |
|---|---|
| `--server URL` | Custom locator server (default: `https://p.projectearth.dev`) |
| `--out DIR, -o DIR` | Output directory (default: current dir) |
| `--keystore PATH` | Signing keystore (default: bundled `resources/earth_test.p12`) |
| `--apktool PATH` | Explicit path to apktool.jar or apktool executable |
| `--jdk PATH` | Explicit path to JDK home |
| `--skip-download` | Use previously cached patches |

## How it works

1. **Validate** — checks the APK contains `libgenoa.so` and a known version code
2. **Download patches** — fetches the latest `.patch` files from [Project-Earth-Team/Patches](https://github.com/Project-Earth-Team/Patches)
3. **Decompile** — runs apktool to decompile the APK
4. **Patch binary** — writes the server address into `libgenoa.so` and fixes a sunset check
5. **Apply patches** — applies smali, resource, and binary patches
6. **Recompile** — rebuilds the APK with apktool
7. **Sign** — signs with jarsigner (from the bundled JDK)

## License

GNU General Public License v3.0. See [LICENSE](LICENSE).

Derived from [Project-Earth-Team/PatcherApp](https://github.com/Project-Earth-Team/PatcherApp).
