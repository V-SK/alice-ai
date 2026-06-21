"""Pin the gateway honest-render helpers (static/lean/alice-gateway.js).

Driven through `node` (same approach as test_match_model_key_js.py); skips when
`node` is not installed. These are the SAME honest-render invariants the web
portal (alice-website/chat.html) ships, ported into the desktop lean app:

  * normalizeModel maps {status} -> selectable ONLY for `ready`; every other
    status (loading / capacity_available / no_capable_node) is non-ready and
    therefore non-selectable.
  * BACK-COMPAT: a `served` bool (pre-status gateway) maps served->ready; an
    object with NEITHER status NOR served is conservatively non-ready, and a
    whole catalog with no signal at all reports signalled=false so the caller
    keeps its static fallback (never marks every tier dead).
  * parseTierError surfaces the gateway's OWN plaintext + retry on the two
    tier-status 503 reason codes, and returns null for any other 503 (so the
    caller raises a generic transport error — never swallows or fabricates).
  * receiptRows is order-stable, drops empties, and defaults paid_acu to '0'.
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
    """Eval an expression against the CommonJS-exported helper, print JSON."""
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


# ---- normalizeModel: status -> selectability (the honest-render core) ------ #

def test_ready_is_the_only_selectable_status():
    out = _run(
        "return ['ready','loading','capacity_available','no_capable_node'].map(function(s){"
        "  var m = G.normalizeModel({id:'t', status:s, parameter_billions:27});"
        "  return {status:m.status, selectable:G.statusPres(m.status).selectable,"
        "          live:m.live, statusKnown:m.statusKnown, signalled:m.signalled,"
        "          label:G.statusPres(m.status).label, params:m.params};"
        "});"
    )
    by = {r["status"]: r for r in out}
    assert by["ready"]["selectable"] is True and by["ready"]["live"] is True
    assert by["ready"]["label"] == "LIVE"
    for s in ("loading", "capacity_available", "no_capable_node"):
        assert by[s]["selectable"] is False, s
        assert by[s]["live"] is False, s
    # param tag derives from parameter_billions
    assert by["ready"]["params"] == "27B"
    # every #91-status object is statusKnown + signalled
    assert all(r["statusKnown"] and r["signalled"] for r in out)


def test_unknown_status_falls_back_to_non_selectable():
    out = _run("var m=G.normalizeModel({id:'x', status:'wat'}); return {status:m.status, sel:G.statusPres(m.status).selectable};")
    assert out["status"] == "capacity_available" and out["sel"] is False


# ---- BACK-COMPAT: served bool / no-signal shapes --------------------------- #

def test_served_bool_backcompat():
    out = _run(
        "return [true,false].map(function(b){ var m=G.normalizeModel({id:'t', served:b});"
        " return {status:m.status, sel:G.statusPres(m.status).selectable, statusKnown:m.statusKnown, signalled:m.signalled}; });"
    )
    served_true, served_false = out
    assert served_true["status"] == "ready" and served_true["sel"] is True
    assert served_false["status"] == "capacity_available" and served_false["sel"] is False
    # served bool is NOT #91-status, but IS a signal
    assert served_true["statusKnown"] is False and served_true["signalled"] is True


def test_no_signal_object_is_conservative_and_unsignalled():
    out = _run("var m=G.normalizeModel({id:'bare', name:'Alice'}); return {status:m.status, sel:G.statusPres(m.status).selectable, signalled:m.signalled, name:m.name};")
    assert out["status"] == "capacity_available" and out["sel"] is False
    assert out["signalled"] is False and out["name"] == "Alice"


def test_pre_deploy_catalog_reports_unsignalled():
    # A whole catalog with NO status/served on any object -> signalled=false, so
    # the caller keeps its static fallback rather than marking every tier dead.
    out = _run(
        "var c=G.normalizeCatalog({object:'list', data:[{id:'a',name:'A'},{id:'b',name:'B'}]});"
        " return {n:c.models.length, signalled:c.signalled, statusKnown:c.statusKnown};"
    )
    assert out["n"] == 2 and out["signalled"] is False and out["statusKnown"] is False


def test_mixed_catalog_firstSelectable_picks_ready():
    out = _run(
        "var c=G.normalizeCatalog({data:[{id:'a',status:'loading'},{id:'b',status:'ready'},{id:'c',status:'ready'}]});"
        " var f=G.firstSelectable(c.models); return {first:f&&f.id, signalled:c.signalled};"
    )
    assert out["first"] == "b" and out["signalled"] is True


def test_firstSelectable_none_when_no_ready():
    out = _run("return G.firstSelectable(G.normalizeCatalog({data:[{id:'a',status:'loading'},{id:'b',status:'no_capable_node'}]}).models);")
    assert out is None


# ---- parseTierError: honest 503 surfacing (never swallow / fabricate) ------ #

def test_503_loading_yields_retryable_tier_error():
    out = _run(
        "var body=JSON.stringify({error:{code:'api_chat_model_tier_loading', message:'Spinning up the 27B tier; retry shortly.'},"
        " metadata:{model_tier_status:'loading'}});"
        " var e=G.parseTierError(503, body, '12');"
        " return e && {tier:e.tier, status:e.status, code:e.code, retryAfter:e.retryAfter, message:e.message};"
    )
    assert out["tier"] is True
    assert out["status"] == "loading"
    assert out["code"] == "api_chat_model_tier_loading"
    assert out["retryAfter"] == 12
    assert "27B" in out["message"]  # the gateway's OWN plaintext, not invented


def test_503_no_capable_node_is_offline_no_retry_default():
    out = _run(
        "var body=JSON.stringify({error:{code:'api_chat_model_tier_no_capable_node', message:'No capable node online.'}});"
        " var e=G.parseTierError(503, body, null);"
        " return e && {status:e.status, code:e.code, retryAfter:e.retryAfter};"
    )
    assert out["status"] == "no_capable_node"
    assert out["code"] == "api_chat_model_tier_no_capable_node"
    assert out["retryAfter"] is None


def test_503_without_known_reason_is_not_a_tier_error():
    # A generic 503 (no tier reason code) must NOT be treated as a tier-status
    # surface -> returns null so the caller raises a real transport error.
    out = _run("return G.parseTierError(503, JSON.stringify({error:{code:'something_else', message:'boom'}}), null);")
    assert out is None


def test_non_503_is_never_a_tier_error():
    out = _run("return [G.parseTierError(500,'x',null), G.parseTierError(200,'',null), G.parseTierError(503,'',null)];")
    assert out == [None, None, None]


# ---- receiptRows: order-stable, drops empties, paid_acu default '0' -------- #

def test_receipt_rows_full_and_paid_acu_default():
    out = _run(
        "return G.receiptRows({spec_id:'spec:qwen3-4b@1', miner_id:'m1', decode_rule:'greedy',"
        " output_token_ids_hash:'abc', request_id:'r1', signed:true, token_ids_available:false});"
    )
    keys = [kv[0] for kv in out]
    # order preserved, empties (miner unset etc.) dropped, paid_acu defaulted
    assert keys[0] == "spec_id"
    assert ["paid_acu", "0"] in out
    assert ["signed", "true"] in out
    assert ["token_ids_available", "false"] in out
    # absent fields (prompt_token_ids_hash, output_cid, nonce) are not present
    assert "prompt_token_ids_hash" not in keys
    assert "nonce" not in keys


def test_receipt_from_chunk_top_level_and_nested():
    out = _run(
        "return {top:G.receiptFromChunk({alice_receipt:{spec_id:'s'}}),"
        " nested:G.receiptFromChunk({choices:[{alice_receipt:{spec_id:'n'}}]}),"
        " none:G.receiptFromChunk({choices:[{delta:{content:'hi'}}]})};"
    )
    assert out["top"]["spec_id"] == "s"
    assert out["nested"]["spec_id"] == "n"
    assert out["none"] is None


def test_login_challenge_text_is_portable():
    out = _run("return G.buildLoginChallenge('alice1abc', 'aliceprotocol.org').challenge;")
    assert out.startswith("Sign in with Alice\n")
    assert "domain: aliceprotocol.org" in out
    assert "address: alice1abc" in out
    assert "will not trigger a transaction" in out
