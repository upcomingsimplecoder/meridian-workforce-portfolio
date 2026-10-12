import json
import shutil
import subprocess
from pathlib import Path


def test_real_reporter_persists_failures_and_frames_without_assertion_values(tmp_path):
    source = Path(__file__).resolve().parents[2] / "scripts/playwright-evidence.mjs"
    module = json.dumps(source.as_uri())
    sentinel = "private-cookie-and-response-sentinel"
    program = """
      import { resolve } from 'node:path';
      const {default: Reporter} = await import(MODULE);
      const secret = SENTINEL;
      const reporter = new Reporter();
      const test = {title: secret, location: {file:resolve('tests/e2e/security.spec.ts'),line:9},parent:{project:()=>({name:'chromium'})}};
      reporter.onBegin({}, {allTests:()=>[test]});
      reporter.onTestEnd(test, {status:'failed',retry:0,duration:12,errors:[{message:'expect(...) '+secret,stack:'Error: '+secret+'\\n at /private/project/tests/e2e/security.spec.ts:9:4'}]});
      reporter.onEnd({status:'failed'});
    """.replace("MODULE", module).replace("SENTINEL", json.dumps(sentinel))
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("Node is required for the real reporter test")
    run = subprocess.run(
        [node, "--input-type=module", "-e", program],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    path = tmp_path / ".local/e2e-case-results.json"
    text = path.read_text()
    record = json.loads(text)
    assert sentinel not in text and sentinel not in run.stdout and sentinel not in run.stderr
    assert record["complete"] is True and record["result"] == "failed"
    assert record["declared_cases"] == 1
    assert record["cases"][0]["status"] == "failed"
    assert record["cases"][0]["file"] == "tests/e2e/security.spec.ts"
    assert record["cases"][0]["diagnostic"][0]["frames"] == [
        {"file": "tests/e2e/security.spec.ts", "line": 9, "column": 4}
    ]
    assert path.stat().st_mode & 0o777 == 0o600
