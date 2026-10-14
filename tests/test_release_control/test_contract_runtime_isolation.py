"""Contract pytest must not invoke the live integration runtime restart helper."""
import os
from pathlib import Path
import shlex
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]

def test_contract_subtree_never_invokes_configured_runtime(tmp_path):
    marker=tmp_path/'runtime-called'
    guard=tmp_path/'runtime-guard'
    guard.write_text('#!/bin/sh\nprintf called > '+shlex.quote(str(marker))+'\nexit 1\n')
    guard.chmod(0o700)
    env=dict(os.environ,BASE_URL='http://127.0.0.1:9',ODIN_CONTAINER_EXEC=str(guard))
    result=subprocess.run([sys.executable,'-m','pytest','-q','tests/test_contracts/test_license_tier_claim_acceptance.py','-k','community'],cwd=ROOT,env=env,capture_output=True,text=True,timeout=30)
    assert result.returncode==0, result.stdout[-1500:]+result.stderr[-1500:]
    assert not marker.exists(), 'Owned contract tests invoked the live runtime restart boundary'
