#!/usr/bin/env bash
#
# build_app.sh — assemble dist/AliceAI-macos-arm64.dmg from the frozen backend
#                (M2 — the one-click macOS package).
#
# Pipeline (all on macOS arm64, pure system tooling + PyInstaller):
#   1. render the AliceAI.icns icon (dark squircle + Alice mark, Miner-family).
#   2. PyInstaller one-dir freeze of the ONE binary (shell + backend roles) —
#      backend/alice-backend.spec → backend/dist/AliceAI/ (mlx Metal libs +
#      transformers tokenizer + the bundled odysseus/static + alice_acp source).
#   3. wrap it into AliceAI.app: the frozen one-dir lives in Contents/MacOS/
#      (binary + _internal side by side — the PyInstaller one-dir convention),
#      Info.plist (id org.aliceprotocol.ai, CFBundleIconFile), the icns.
#   4. INNER-FIRST ad-hoc codesign: sign every nested dylib/.so/.metallib +
#      the frozen binary BEFORE sealing the bundle (NOT --deep, which signs in
#      the wrong order and is deprecated). UNSIGNED in the Apple-cert sense
#      (ad-hoc "-" identity, like the Wallet/Miner) — no Developer ID, no
#      notarization (the ed25519 update manifest is the trust anchor later).
#   5. package AliceAI.app → dist/AliceAI-macos-arm64.dmg (hdiutil) + a .zip.
#
# Models are NOT bundled (the installer stays lean) — first run downloads Alice
# Lite to ~/.alice/models.
#
# Usage:  packaging/macos/build_app.sh            (full: freeze + app + dmg)
#         SKIP_FREEZE=1 packaging/macos/build_app.sh   (reuse backend/dist/AliceAI)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BACKEND="${ROOT_DIR}/backend"
VENV="${ROOT_DIR}/.venv"
PY="${VENV}/bin/python"
PYINSTALLER="${VENV}/bin/pyinstaller"

APP_NAME="AliceAI"
BUNDLE_ID="org.aliceprotocol.ai"
DISPLAY_NAME="Alice"
VERSION="0.1.0"
ARCH="arm64"

DIST="${ROOT_DIR}/dist"
APP="${DIST}/${APP_NAME}.app"
ICNS="${ROOT_DIR}/assets/icons/${APP_NAME}.icns"
FROZEN_APP="${BACKEND}/dist/${APP_NAME}.app"  # PyInstaller BUNDLE output
DMG="${DIST}/${APP_NAME}-macos-${ARCH}.dmg"
ZIP="${DIST}/${APP_NAME}-macos-${ARCH}.zip"

[[ "$(uname)" == "Darwin" ]] || { echo "error: build on macOS" >&2; exit 1; }
[[ -x "${PY}" ]] || { echo "error: no venv python at ${PY}" >&2; exit 1; }

echo "==> 1/5  icon"
bash "${ROOT_DIR}/packaging/macos/build_icon.sh" >/dev/null
[[ -f "${ICNS}" ]] || { echo "error: icon build produced no ${ICNS}" >&2; exit 1; }
echo "    ${ICNS}"

echo "==> 2/5  PyInstaller freeze (emits ${APP_NAME}.app via BUNDLE)"
if [[ "${SKIP_FREEZE:-0}" == "1" && -d "${FROZEN_APP}" ]]; then
  echo "    SKIP_FREEZE=1 — reusing ${FROZEN_APP}"
else
  ( cd "${BACKEND}" && rm -rf build dist && "${PYINSTALLER}" --noconfirm --log-level=WARN alice-backend.spec )
fi
EXE_IN_APP="${FROZEN_APP}/Contents/MacOS/${APP_NAME}"
[[ -x "${EXE_IN_APP}" ]] || { echo "error: freeze produced no ${EXE_IN_APP}" >&2; exit 1; }
# Assert arm64 (F7 — wrong-arch on Apple Silicon is a known upstream foot-gun).
if ! lipo -archs "${EXE_IN_APP}" | grep -q "${ARCH}"; then
  echo "error: frozen binary is not ${ARCH} (got: $(lipo -archs "${EXE_IN_APP}"))" >&2
  exit 1
fi
echo "    ${FROZEN_APP} ($(du -sh "${FROZEN_APP}" | cut -f1)), arch=$(lipo -archs "${EXE_IN_APP}")"

echo "==> 3/5  stage ${APP_NAME}.app in dist/"
rm -rf "${APP}"
mkdir -p "${DIST}"
cp -R "${FROZEN_APP}" "${APP}"

echo "==> 4/5  inner-first ad-hoc codesign (UNSIGNED / no Apple cert)"
# Sign EVERY nested Mach-O (dylib/.so/.metallib + any helper exe) FIRST, then
# the main binary, then SEAL the bundle last. This bottom-up order is what
# --deep gets wrong; doing it by hand keeps Gatekeeper's seal valid for an
# ad-hoc identity. "-" = ad-hoc (no Developer ID) — matches the Wallet/Miner.
SIGN_ID="-"
# 4a) nested Mach-O objects (deepest first via reverse-depth sort). PyInstaller's
#     BUNDLE puts the dylib/.so/.metallib under Contents/Frameworks; sign every
#     one bottom-up so the bundle seal in 4c is valid.
find "${APP}/Contents" \( -name '*.dylib' -o -name '*.so' -o -name '*.metallib' \) -type f \
  | awk '{ print gsub(/\//,"/"), $0 }' | sort -rn | cut -d' ' -f2- \
  | while IFS= read -r lib; do
      codesign --force --timestamp=none --sign "${SIGN_ID}" "${lib}" 2>/dev/null || \
      codesign --force --sign "${SIGN_ID}" "${lib}"
    done
# 4b) the main frozen executable.
codesign --force --sign "${SIGN_ID}" "${EXE_IN_APP/${FROZEN_APP}/${APP}}"
# 4c) seal the whole bundle (entitlements: none — ad-hoc local app).
codesign --force --sign "${SIGN_ID}" "${APP}"
# Verify the seal (ad-hoc verify; Gatekeeper assessment will still warn since
# there's no Developer ID — that's expected + documented).
codesign --verify --verbose=2 "${APP}" 2>&1 | sed 's/^/    /' || true
echo "    codesigned ad-hoc (identity '-')"

echo "==> 5/5  package .dmg + .zip"
rm -f "${DMG}" "${ZIP}"
# .dmg via hdiutil (drag-to-Applications); UDZO = compressed.
TMP_DMG_DIR="$(mktemp -d)"
cp -R "${APP}" "${TMP_DMG_DIR}/"
ln -s /Applications "${TMP_DMG_DIR}/Applications"
hdiutil create -volname "${DISPLAY_NAME}" -srcfolder "${TMP_DMG_DIR}" \
  -ov -format UDZO "${DMG}" >/dev/null
rm -rf "${TMP_DMG_DIR}"
# .zip fallback (ditto preserves the codesign + symlinks).
( cd "${DIST}" && ditto -c -k --sequesterRsrc --keepParent "${APP_NAME}.app" "$(basename "${ZIP}")" )

echo ""
echo "DONE:"
echo "  app : ${APP}    ($(du -sh "${APP}" | cut -f1))"
echo "  dmg : ${DMG}    ($(du -h "${DMG}" | cut -f1))"
echo "  zip : ${ZIP}    ($(du -h "${ZIP}" | cut -f1))"
echo ""
echo "Launch (double-click target):  open '${APP}'"
echo "Unsigned (ad-hoc) — first launch may need: right-click → Open, or"
echo "  xattr -dr com.apple.quarantine '${APP}'   (the .app is not quarantined when built locally)"
