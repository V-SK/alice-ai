"""Pin the gateway send-gate decision (static/lean/alice-gateway.js dispatchModel).

This is the pure, node-testable core of the Q3 must-fix: in gateway mode the
lean composer (static/lean/alice-lean.js gatewayChatModel/onSend) dispatches the
wire model id returned by AliceGateway.dispatchModel. The invariants pinned here:

  * NO selectable tier  -> dispatchModel returns None (null). The send path
    treats null as "refuse to dispatch" — it NEVER falls back to gwModels[0].id
    or the literal 'alice'. (防吹牛: serve only against a verifiably LIVE tier.)
  * A selected tier that is NO LONGER selectable -> snaps to the first
    selectable tier (never dispatches against the stale non-ready selection).
  * A selected tier that IS still selectable -> kept verbatim.
  * No selection but a ready tier exists -> first selectable.

Driven through `node` (same harness as test_gateway_normalize_js.py); skips when
`node` is absent.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_HELPER = (_REPO / "static" / "lean" / "alice-gateway.js").as_posix()
_HAS_NODE = shutil.which("node") is not None

pytestmark = pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")


def _run(expr_js):
    js = (
        "const G = require(%s);\n" % json.dumps(_HELPER)
        + "const out = (function(){ %s })();\n" % expr_js
        + "process.stdout.write(JSON.stringify(out));\n"
    )
    proc = subprocess.run(
        ["node", "--input-type=commonjs"],
        input=js, capture_output=True, text=True, cwd=str(_REPO), timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())


def _catalog(js_data):
    return "G.normalizeCatalog({data:%s}).models" % js_data


# ---- THE must-fix: no selectable tier => no dispatch (null, no fallback) ---- #

def test_no_selectable_tier_yields_null_no_fallback():
    # Two tiers, BOTH non-ready (loading + no_capable_node), and a stale
    # selection pointing at one of them. The OLD bug fell back to gwModels[0].id
    # ('a') or the literal 'alice'; the fix must return null instead.
    models = _catalog("[{id:'a',status:'loading'},{id:'b',status:'no_capable_node'}]")
    out = _run(
        "return {selected:G.dispatchModel('a', %s),"
        " none:G.dispatchModel(null, %s),"
        " bogus:G.dispatchModel('alice', %s)};" % (models, models, models)
    )
    # No live tier => refuse, regardless of what was selected.
    assert out["selected"] is None
    assert out["none"] is None
    assert out["bogus"] is None
    # And crucially NOT the first catalog id nor the literal placeholder.
    assert out["selected"] != "a" and out["selected"] != "alice"


def test_empty_or_missing_catalog_yields_null():
    out = _run(
        "return {empty:G.dispatchModel('x', []),"
        " undef:G.dispatchModel('x', undefined),"
        " nullsel:G.dispatchModel(null, [])};"
    )
    assert out == {"empty": None, "undef": None, "nullsel": None}


# ---- stale selection self-heals to the first selectable tier ---------------- #

def test_stale_nonselectable_selection_snaps_to_first_ready():
    models = _catalog("[{id:'a',status:'loading'},{id:'b',status:'ready'},{id:'c',status:'ready'}]")
    # 'a' is selected but loading (non-selectable) -> must pick first ready 'b'.
    out = _run("return G.dispatchModel('a', %s);" % models)
    assert out == "b"


def test_no_selection_picks_first_selectable():
    models = _catalog("[{id:'a',status:'capacity_available'},{id:'b',status:'ready'}]")
    out = _run("return G.dispatchModel(null, %s);" % models)
    assert out == "b"


# ---- a still-selectable selection is kept verbatim -------------------------- #

def test_selectable_selection_kept():
    models = _catalog("[{id:'a',status:'ready'},{id:'b',status:'ready'}]")
    out = _run("return G.dispatchModel('a', %s);" % models)
    assert out == "a"
