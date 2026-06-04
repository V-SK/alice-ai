#!/usr/bin/env bash
#
# vendor_odysseus.sh — vendor the MIT odysseus fork into backend/odysseus/,
# pinned to a SPECIFIC upstream commit, MIT-compliant, with the Docker/compose/
# service files stripped (PLAN §2.3 — we never use them).
#
# Re-vendoring (re-pinning) is a deliberate, reviewed step (PLAN R9): bump
# ODYSSEUS_PINNED_SHA below, re-run, and review the diff. Our own code is
# import-isolated from odysseus internals, so drift is contained.
#
# Usage:
#   scripts/vendor_odysseus.sh                 # clone from $ODYSSEUS_REMOTE
#   ODYSSEUS_CLONE_SOURCE=/path/to/checkout \
#     scripts/vendor_odysseus.sh               # clone from a local checkout (offline/fast)
#
set -euo pipefail

# ---- pin (the whole point of this script) -----------------------------------
# Pinned to the survey checkout HEAD that was verified against the brief
# (PLAN grounding note, 2026-06-04). DO NOT float to upstream HEAD.
ODYSSEUS_PINNED_SHA="4dc11cfe6b57105511bb414d080a9043b643c091"
ODYSSEUS_REMOTE="https://github.com/pewdiepie-archdaemon/odysseus"

# A local checkout to clone from instead of the network (optional). The survey
# checkout is the default if it exists; falls back to the remote otherwise.
ODYSSEUS_CLONE_SOURCE="${ODYSSEUS_CLONE_SOURCE:-/tmp/odysseus-survey}"

# ---- paths ------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEST="${REPO_ROOT}/backend/odysseus"
NOTICE_FILE="${REPO_ROOT}/NOTICE"

echo "==> Vendoring odysseus @ ${ODYSSEUS_PINNED_SHA}"
echo "    dest: ${DEST}"

# ---- pick a clone source ----------------------------------------------------
SRC="${ODYSSEUS_REMOTE}"
if [ -d "${ODYSSEUS_CLONE_SOURCE}/.git" ]; then
  # Verify the local checkout actually has the pinned commit before trusting it.
  if git -C "${ODYSSEUS_CLONE_SOURCE}" cat-file -e "${ODYSSEUS_PINNED_SHA}^{commit}" 2>/dev/null; then
    SRC="${ODYSSEUS_CLONE_SOURCE}"
    echo "    source: local checkout ${ODYSSEUS_CLONE_SOURCE} (has the pinned commit)"
  else
    echo "    source: ${ODYSSEUS_REMOTE} (local checkout lacks the pinned commit)"
  fi
else
  echo "    source: ${ODYSSEUS_REMOTE}"
fi

# ---- fresh clone into a temp dir, then checkout the exact SHA ---------------
TMP="$(mktemp -d)"
cleanup() { rm -rf "${TMP}"; }
trap cleanup EXIT

git clone --quiet --no-checkout "${SRC}" "${TMP}/odysseus"
# Ensure the pinned object is present (a shared/local clone may be shallow-ish);
# fetch it explicitly from the canonical remote if missing.
if ! git -C "${TMP}/odysseus" cat-file -e "${ODYSSEUS_PINNED_SHA}^{commit}" 2>/dev/null; then
  echo "==> Pinned commit not in clone; fetching from ${ODYSSEUS_REMOTE}"
  git -C "${TMP}/odysseus" fetch --quiet "${ODYSSEUS_REMOTE}" "${ODYSSEUS_PINNED_SHA}" \
    || git -C "${TMP}/odysseus" fetch --quiet "${ODYSSEUS_REMOTE}"
fi
git -C "${TMP}/odysseus" checkout --quiet "${ODYSSEUS_PINNED_SHA}"

RESOLVED_SHA="$(git -C "${TMP}/odysseus" rev-parse HEAD)"
if [ "${RESOLVED_SHA}" != "${ODYSSEUS_PINNED_SHA}" ]; then
  echo "ERROR: resolved ${RESOLVED_SHA} != pinned ${ODYSSEUS_PINNED_SHA}" >&2
  exit 1
fi
echo "==> Checked out ${RESOLVED_SHA}"

# Capture provenance before we drop the .git dir.
COMMIT_SUBJECT="$(git -C "${TMP}/odysseus" log -1 --format='%s')"
COMMIT_DATE="$(git -C "${TMP}/odysseus" log -1 --format='%ci')"

# ---- publish into backend/odysseus/ (drop upstream .git; this repo owns it) -
rm -rf "${DEST}"
mkdir -p "$(dirname "${DEST}")"
cp -R "${TMP}/odysseus" "${DEST}"
rm -rf "${DEST}/.git"

# ---- STRIP Docker / compose / systemd-service files (PLAN §2.3) -------------
# We never run odysseus in a container or as a service; our engine owns serving.
echo "==> Stripping Docker / compose / service files (PLAN §2.3)"
rm -rf \
  "${DEST}/docker" \
  "${DEST}/Dockerfile" \
  "${DEST}/.dockerignore" \
  "${DEST}/install-service.sh" 2>/dev/null || true
# Globs for the compose + unit files (names vary: gpu-amd / gpu-nvidia / *.service).
find "${DEST}" -maxdepth 1 -type f \( \
  -name 'docker-compose*.yml' -o \
  -name 'docker-compose*.yaml' -o \
  -name '*.service' \
\) -print -delete || true

# ---- MIT compliance: stamp provenance into NOTICE ---------------------------
# The verbatim odysseus LICENSE already lives in NOTICE (committed at M0). Here
# we (a) assert the LICENSE file is present in the vendored tree, and (b) keep
# the pinned-SHA line in NOTICE in sync with this script.
if [ ! -f "${DEST}/LICENSE" ]; then
  echo "WARNING: vendored odysseus has no LICENSE file — MIT requires it." >&2
fi
if [ -f "${NOTICE_FILE}" ]; then
  # Keep NOTICE's "Pinned commit:" line matching this script (portable in-place sed).
  tmp_notice="$(mktemp)"
  sed "s/^Pinned commit:.*/Pinned commit:   ${RESOLVED_SHA}/" "${NOTICE_FILE}" > "${tmp_notice}"
  mv "${tmp_notice}" "${NOTICE_FILE}"
  echo "==> NOTICE pinned-commit line synced to ${RESOLVED_SHA}"
fi

# ---- record what we vendored (a small breadcrumb in the fork dir) -----------
cat > "${DEST}/VENDORED.txt" <<EOF
Vendored from: ${ODYSSEUS_REMOTE}
Pinned commit: ${RESOLVED_SHA}
Commit:        ${COMMIT_SUBJECT}
Commit date:   ${COMMIT_DATE}
Vendored by:   scripts/vendor_odysseus.sh
Stripped:      docker/, Dockerfile, .dockerignore, docker-compose*.yml, *.service,
               install-service.sh  (PLAN §2.3 — never used)

This tree is otherwise kept as-is; the trim/strip/skin of routes + frontend
happens in later milestones. Upstream MIT LICENSE is retained here and in
../../NOTICE.
EOF

echo "==> Done. backend/odysseus/ populated @ ${RESOLVED_SHA}"
echo "    (Docker/compose/service files stripped; LICENSE retained.)"
