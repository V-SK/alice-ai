#!/usr/bin/env bash
#
# build_appimage.sh — assemble the UNSIGNED Linux x64 Alice AI .AppImage (M6).
#
# Linux counterpart of packaging/macos/build_app.sh. Same ONE frozen binary, two
# roles (PLAN §2.2): the PyWebView shell (default) re-execs itself as the uvicorn
# backend child; on POSIX the child is in its own session/process-group and the
# shell SIGTERM/SIGKILLs the group on quit (supervisor.py — shared with macOS).
#
# Pipeline (Linux x64, PyInstaller + appimagetool):
#   1. deps: compile + hash-install the Linux set (mlx→llama-cpp-python swap):
#      backend/requirements.linux.txt → backend/requirements.lock.linux.txt.
#   2. PyInstaller one-dir freeze (the same backend/alice-backend.spec; on Linux
#      it stops at COLLECT — no macOS BUNDLE) → backend/dist/AliceAI/.
#   3. stage an AppDir: the one-dir under usr/, an AppRun launcher, a .desktop,
#      and the icon.
#   4. **vendor webkit2gtk + GTK runtime libs into the AppDir** so PyWebView's
#      WebKit2 webview works on a clean machine with NO `apt install`
#      (the one real PyWebView-on-Linux gap, PLAN §2.3). We copy the resolved
#      libwebkit2gtk-4.1 / libgtk-3 / libjavascriptcoregtk dependency closure +
#      the WebKitNetworkProcess/WebKitWebProcess helpers into usr/lib, and point
#      LD_LIBRARY_PATH + WEBKIT_EXEC_PATH at them from AppRun.
#   5. appimagetool AppDir → dist/AliceAI-linux-x86_64.AppImage. UNSIGNED
#      (no GPG by default; --sign to embed a detached sig if a key is set).
#
# Models are NOT bundled (AppImage stays lean) — first run downloads Alice Lite
# to the user data dir (~/.alice/models; override via ALICE_AI_DATA_DIR / XDG).
#
# Usage:
#   packaging/linux/build_appimage.sh             # full build (CPU runtime)
#   CUDA=1 packaging/linux/build_appimage.sh      # CUDA llama-cpp-python wheel
#   SKIP_FREEZE=1 packaging/linux/build_appimage.sh   # reuse backend/dist/AliceAI
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BACKEND="${ROOT_DIR}/backend"
DIST="${ROOT_DIR}/dist"
APP_NAME="AliceAI"
ARCH="x86_64"
FROZEN_DIR="${BACKEND}/dist/${APP_NAME}"          # PyInstaller COLLECT one-dir
APPDIR="${DIST}/${APP_NAME}.AppDir"
APPIMAGE="${DIST}/${APP_NAME}-linux-${ARCH}.AppImage"
ICON_PNG="${ROOT_DIR}/assets/brand/alice-logo.png"
REQ_LINUX="${BACKEND}/requirements.linux.txt"
LOCK_LINUX="${BACKEND}/requirements.lock.linux.txt"

[[ "$(uname)" == "Linux" ]] || { echo "error: build_appimage.sh must run on Linux" >&2; exit 1; }

# Python: prefer the in-repo venv, else system python3.
if [[ -x "${ROOT_DIR}/.venv/bin/python" ]]; then PY="${ROOT_DIR}/.venv/bin/python"; else PY="python3"; fi
echo "==> python: ${PY}"

# UTF-8 for the build session (llama.cpp/HF progress); the entry sets it for run.
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8

# --------------------------------------------------------------------------- #
# 1/5  deps — hash-pinned Linux lock (the mlx→llama_cpp swap)
# --------------------------------------------------------------------------- #
echo "==> 1/5  Linux deps (llama-cpp-python; mlx omitted)"
if [[ "${SKIP_FREEZE:-0}" != "1" ]]; then
  "${PY}" -m pip install --quiet --upgrade uv
  if [[ "${CUDA:-0}" == "1" ]]; then
    echo "    CUDA=1: resolving the CUDA llama-cpp-python wheel"
    export PIP_EXTRA_INDEX_URL="https://abetlen.github.io/llama-cpp-python/whl/cu124"
  fi
  # Generate the hash-pinned lock ON THIS LINUX RUNNER (manylinux wheel hashes;
  # a macOS lock can't install here). Committed by CI. Run from backend/ so the
  # input's editable `-e ../../alice-acp` path dep resolves against the sibling
  # repo (pip/uv resolve relative paths against CWD, not the -r file's dir).
  ( cd "${BACKEND}" \
      && "${PY}" -m uv pip compile --generate-hashes --no-header \
           requirements.linux.txt -o requirements.lock.linux.txt \
      && "${PY}" -m pip install --quiet -e ../../alice-acp \
      && "${PY}" -m pip install --quiet --require-hashes -r requirements.lock.linux.txt )
  [[ -f "${LOCK_LINUX}" ]] || { echo "error: lock not generated: ${LOCK_LINUX}" >&2; exit 1; }
else
  echo "    SKIP_FREEZE=1 — assuming deps already installed"
fi
# Sanity: the runtime swap must hold (llama_cpp present, mlx absent).
"${PY}" -c "import llama_cpp; print('    llama_cpp', llama_cpp.__version__)"
"${PY}" -c "import importlib.util,sys; sys.exit(1 if importlib.util.find_spec('mlx') else 0)" \
  || echo "    note: mlx present on Linux (unexpected) — never selected by the probe"

# --------------------------------------------------------------------------- #
# 2/5  PyInstaller freeze (one-dir; no BUNDLE on Linux)
# --------------------------------------------------------------------------- #
echo "==> 2/5  PyInstaller freeze"
if [[ "${SKIP_FREEZE:-0}" == "1" && -d "${FROZEN_DIR}" ]]; then
  echo "    SKIP_FREEZE=1 — reusing ${FROZEN_DIR}"
else
  ( cd "${BACKEND}" && rm -rf build dist && "${PY}" -m PyInstaller --noconfirm --log-level=WARN alice-backend.spec )
fi
EXE="${FROZEN_DIR}/${APP_NAME}"
[[ -x "${EXE}" ]] || { echo "error: freeze produced no ${EXE}" >&2; exit 1; }
echo "    ${FROZEN_DIR} ($(du -sh "${FROZEN_DIR}" | cut -f1))"

# --------------------------------------------------------------------------- #
# 3/5  stage the AppDir (one-dir + AppRun + .desktop + icon)
# --------------------------------------------------------------------------- #
echo "==> 3/5  stage AppDir"
rm -rf "${APPDIR}"
mkdir -p "${APPDIR}/usr/bin" "${APPDIR}/usr/lib" "${APPDIR}/usr/share/applications" \
         "${APPDIR}/usr/share/icons/hicolor/256x256/apps"
# The whole one-dir (binary + _internal) into usr/bin/AliceAI-dir, exec is the bin.
cp -r "${FROZEN_DIR}" "${APPDIR}/usr/bin/${APP_NAME}-dir"

# Icon (PNG; AppImage uses it for the desktop entry + thumbnailer).
if [[ -f "${ICON_PNG}" ]]; then
  cp "${ICON_PNG}" "${APPDIR}/${APP_NAME}.png"
  cp "${ICON_PNG}" "${APPDIR}/usr/share/icons/hicolor/256x256/apps/${APP_NAME}.png"
fi

# .desktop (required by appimagetool; Categories=Utility, single-window GUI).
cat > "${APPDIR}/usr/share/applications/${APP_NAME}.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Alice
Comment=Local-AI chat — private, on-device
Exec=AppRun
Icon=${APP_NAME}
Categories=Utility;Network;
Terminal=false
DESKTOP
cp "${APPDIR}/usr/share/applications/${APP_NAME}.desktop" "${APPDIR}/${APP_NAME}.desktop"

# --------------------------------------------------------------------------- #
# 4/5  VENDOR webkit2gtk + GTK into the AppDir (no `apt install` on the user box)
#
# PyWebView's GTK backend dlopen's libwebkit2gtk-4.1 at runtime (it is NOT a
# PyInstaller-traceable import, so the freeze won't pull it). We copy the lib +
# its full dependency closure + the WebKit helper processes into usr/lib and
# point AppRun's LD_LIBRARY_PATH at them. ldd resolves the closure; we filter out
# the truly-core libs (libc/libstdc++/ld-linux/glibc) which must come from the
# host to stay ABI-compatible — bundling those is the classic AppImage footgun.
# --------------------------------------------------------------------------- #
echo "==> 4/5  vendor webkit2gtk + GTK runtime"
vendor_lib() {
  # $1 = an soname or absolute path; copy it + its closure into usr/lib.
  local target="$1" sofile
  sofile="$(ldconfig -p 2>/dev/null | awk -v n="$target" '$1 ~ n {print $NF; exit}')"
  [[ -z "${sofile}" && -e "${target}" ]] && sofile="${target}"
  if [[ -z "${sofile}" || ! -e "${sofile}" ]]; then
    echo "    WARN: ${target} not found on the build host — install it first:"
    echo "          sudo apt-get install -y libwebkit2gtk-4.1-0 libgtk-3-0"
    return 1
  fi
  cp -Lf "${sofile}" "${APPDIR}/usr/lib/" 2>/dev/null || true
  # Copy the dependency closure, skipping host-core libs.
  ldd "${sofile}" 2>/dev/null | awk '{print $3}' | grep -E '^/' | while read -r dep; do
    case "$(basename "${dep}")" in
      libc.so.*|libstdc++.so.*|libm.so.*|libdl.so.*|libpthread.so.*|ld-linux*|libgcc_s.so.*|librt.so.*) ;;
      *) cp -Lf "${dep}" "${APPDIR}/usr/lib/" 2>/dev/null || true ;;
    esac
  done
}
WK_OK=1
vendor_lib "libwebkit2gtk-4.1" || WK_OK=0
vendor_lib "libgtk-3" || true
vendor_lib "libjavascriptcoregtk-4.1" || true
vendor_lib "libsoup-3.0" || true
# WebKit spawns helper processes (WebKitNetworkProcess / WebKitWebProcess) found
# via a libexec dir; copy the dir and export WEBKIT_EXEC_PATH at runtime.
for d in /usr/lib/x86_64-linux-gnu/webkit2gtk-4.1 /usr/libexec/webkit2gtk-4.1 \
         /usr/lib/webkit2gtk-4.1; do
  if [[ -d "${d}" ]]; then
    mkdir -p "${APPDIR}/usr/lib/webkit2gtk-4.1"
    cp -rL "${d}/." "${APPDIR}/usr/lib/webkit2gtk-4.1/" 2>/dev/null || true
    break
  fi
done
# GDK pixbuf loaders + GIO modules (PNG/SVG icon rendering, TLS for HF download).
for gdir in /usr/lib/x86_64-linux-gnu/gdk-pixbuf-2.0 /usr/lib/gdk-pixbuf-2.0; do
  [[ -d "${gdir}" ]] && { mkdir -p "${APPDIR}/usr/lib/gdk-pixbuf-2.0"; cp -rL "${gdir}/." "${APPDIR}/usr/lib/gdk-pixbuf-2.0/" 2>/dev/null || true; break; }
done
[[ "${WK_OK}" == "1" ]] && echo "    webkit2gtk vendored ($(ls "${APPDIR}/usr/lib" | wc -l) libs)" \
  || echo "    WARN: webkit2gtk NOT vendored — the AppImage will need it on the host"

# AppRun: set up the bundled-lib env, then exec the frozen shell. The frozen
# binary defaults to the shell role (it re-execs itself for the backend child).
cat > "${APPDIR}/AppRun" <<'APPRUN'
#!/usr/bin/env bash
HERE="$(dirname "$(readlink -f "${0}")")"
export LD_LIBRARY_PATH="${HERE}/usr/lib:${LD_LIBRARY_PATH:-}"
export GDK_PIXBUF_MODULEDIR="${HERE}/usr/lib/gdk-pixbuf-2.0/2.10.0/loaders"
export WEBKIT_EXEC_PATH="${HERE}/usr/lib/webkit2gtk-4.1"
# Force the GTK WebView backend (PyWebView would otherwise probe Qt first).
export PYWEBVIEW_GTK=1
exec "${HERE}/usr/bin/AliceAI-dir/AliceAI" "$@"
APPRUN
chmod +x "${APPDIR}/AppRun"

# --------------------------------------------------------------------------- #
# 5/5  appimagetool → .AppImage (UNSIGNED)
# --------------------------------------------------------------------------- #
echo "==> 5/5  appimagetool"
mkdir -p "${DIST}"
rm -f "${APPIMAGE}"
# Locate appimagetool (PATH, or download the static x86_64 build to /tmp).
if command -v appimagetool >/dev/null 2>&1; then
  AITOOL="appimagetool"
else
  AITOOL="/tmp/appimagetool-${ARCH}.AppImage"
  if [[ ! -x "${AITOOL}" ]]; then
    echo "    fetching appimagetool…"
    curl -fsSL -o "${AITOOL}" \
      "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-${ARCH}.AppImage"
    chmod +x "${AITOOL}"
  fi
fi
# ARCH env is what appimagetool stamps into the runtime. --no-appstream keeps it
# dependency-light. UNSIGNED: no --sign unless SIGN=1 + a configured GPG key.
SIGN_ARGS=()
[[ "${SIGN:-0}" == "1" ]] && SIGN_ARGS+=(--sign)
ARCH="${ARCH}" "${AITOOL}" --no-appstream "${SIGN_ARGS[@]}" "${APPDIR}" "${APPIMAGE}" \
  || { echo "error: appimagetool failed (FUSE needed? try '${AITOOL} --appimage-extract-and-run')" >&2; exit 1; }
chmod +x "${APPIMAGE}"

echo ""
echo "DONE (UNSIGNED Linux x64):"
echo "  AppDir   : ${APPDIR}"
echo "  AppImage : ${APPIMAGE}    ($(du -h "${APPIMAGE}" | cut -f1))"
echo ""
echo "Run (no apt install — webkit2gtk/GTK are bundled):"
echo "  chmod +x '${APPIMAGE}' && '${APPIMAGE}'"
echo "  (If FUSE is missing on a minimal host: '${APPIMAGE}' --appimage-extract-and-run)"
