import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const root = new URL('../', import.meta.url);
const selfTest = await readFile(new URL('.github/workflows/phase3-linux-self-test.yml', root), 'utf8');
const controlled = await readFile(new URL('.github/workflows/phase3-linux-controlled.yml', root), 'utf8');
const p403 = await readFile(new URL('.github/workflows/phase4-p403-linux-recovery.yml', root), 'utf8');
const selfTestScript = await readFile(new URL('scripts/phase3-linux-self-test.sh', root), 'utf8');
const controlledScript = await readFile(new URL('scripts/phase3-linux-controlled-runner.sh', root), 'utf8');
const p403Script = await readFile(new URL('scripts/phase4-p403-linux-runner.sh', root), 'utf8');
const p403SelfTestScript = await readFile(new URL('scripts/phase4-p403-runner-self-test.sh', root), 'utf8');
const p403FinalizeScript = await readFile(new URL('scripts/phase4-p403-finalize-linux-run.sh', root), 'utf8');

test('PR and scheduled self-test uses only an unprivileged Linux hosted runner', () => {
  assert.match(selfTest, /pull_request:/);
  assert.match(selfTest, /runs-on: ubuntu-24\.04/);
  assert.doesNotMatch(selfTest, /self-hosted|windows/i);
  assert.match(selfTest, /permissions:\s+contents: read/);
  assert.match(selfTest, /persist-credentials: false/);
  assert.match(selfTest, /if: always\(\)/);
  assert.match(selfTest, /cancel-in-progress: true/);
});

test('controlled worker can only run from an opted-in origin default-branch dispatch', () => {
  assert.match(controlled, /workflow_dispatch:/);
  assert.doesNotMatch(controlled, /pull_request:|schedule:/);
  assert.match(controlled, /github\.event_name == 'workflow_dispatch'/);
  assert.match(controlled, /github\.repository == github\.event\.repository\.full_name/);
  assert.match(controlled, /github\.event\.repository\.fork == false/);
  assert.match(controlled, /github\.ref == format\('refs\/heads\/\{0\}', github\.event\.repository\.default_branch\)/);
  assert.match(controlled, /vars\.PHASE3_LINUX_RUNNER_ENABLED == 'true'/);
  assert.match(controlled, /runs-on: \[self-hosted, linux, x64, ligamina-phase3\]/);
  assert.match(controlled, /environment: phase3-linux-controlled/);
  assert.match(controlled, /cancel-in-progress: false/);
  assert.match(controlled, /persist-credentials: false/);
  assert.match(controlled, /if: always\(\)/);
  assert.match(controlled, /phase3-linux-controlled-runner\.sh/);
  assert.match(controlled, /phase3-p309-linux-controlled-\$\{\{ github\.run_id \}\}\.json/);
});

test('controlled runner records failure and interruption logs around the frozen campaign', () => {
  assert.match(controlledScript, /trap finish EXIT/);
  assert.match(controlledScript, /trap 'exit 130' INT/);
  assert.match(controlledScript, /trap 'exit 143' TERM/);
  assert.match(controlledScript, /tee -a "\$log"/);
  assert.match(controlledScript, /execute-recovery-pilot/);
  assert.match(controlledScript, /"windows_tests_run": False/);
  assert.match(controlledScript, /Ligamina-automation-evidence/);
});

test('P4-03 controlled workflow is separately gated and binds only the reviewed 39-unit parent manifest', () => {
  assert.match(p403, /workflow_dispatch:/);
  assert.match(p403, /runner_contract_success/);
  assert.match(p403, /runner_contract_failure/);
  assert.match(p403, /runner_contract_cancel/);
  assert.match(p403, /linux_unknown_recovery_remaining/);
  assert.doesNotMatch(p403, /pull_request:|schedule:/);
  assert.match(p403, /vars\.PHASE4_P403_RUNNER_ENABLED == 'true'/);
  assert.match(p403, /vars\.PHASE4_P403_LINUX_RECOVERY_ENABLED == 'true'/);
  assert.match(p403, /mode != 'linux_unknown_recovery_remaining'/);
  assert.match(p403, /runs-on: \[self-hosted, linux, x64, ligamina-phase4-p403\]/);
  assert.match(p403, /environment: phase4-p403-linux-controlled/);
  assert.match(p403, /permissions:\s+contents: read/);
  assert.match(p403, /persist-credentials: false/);
  assert.match(p403, /if: always\(\)/);
  assert.match(p403, /cancel-in-progress: false/);
  assert.match(p403, /phase4-p403-linux-execution-manifest-20261027-v3\.json/);
  assert.match(p403, /phase4-p403-linux-execution-manifest-20261027-v4-not-run\.json/);
  assert.match(p403Script, /phase4-p403-recovery-manifest-20261027\.json/);
  assert.match(p403Script, /P4_P403_MODE/);
  assert.match(p403Script, /phase4-p403-runner-self-test\.sh/);
  assert.match(p403Script, /validate-p403-recovery/);
  assert.match(p403Script, /validate-p403-linux-executor/);
  assert.match(p403Script, /run_step linux_worker_probes uv run --locked --project lab --python 3\.12\.14 python/);
  assert.doesNotMatch(p403Script, /run_step linux_worker_probes uv run --locked --project hosts\/nonebot/);
  assert.match(p403Script, /execute-p403-linux-recovery/);
  assert.match(p403Script, /linux_unknown_recovery_remaining/);
  assert.match(p403Script, /phase4-p403-linux-execution-manifest-20261027-v4-not-run\.json/);
  assert.match(p403Script, /trap finish EXIT/);
  assert.match(p403Script, /trap 'exit 130' INT/);
  assert.match(p403Script, /trap 'exit 143' TERM/);
  assert.match(p403Script, /Ligamina-p403-evidence-/);
  assert.match(p403Script, /Ligamina-p403-queue-/);
  assert.doesNotMatch(p403Script, /execute-recovery-pilot/);
  assert.match(p403FinalizeScript, /reconcile-p403-linux-runner/);
  assert.match(p403FinalizeScript, /phase4-p403-linux-execution-manifest-20261027-v4-not-run\.json/);
  assert.match(p403FinalizeScript, /lab\.automation\.phase4_p403_reports/);
  assert.match(p403, /P4_P403_JOB_STATUS: \$\{\{ job\.status \}\}/);
  assert.match(p403FinalizeScript, /Ligamina-p403-queue-/);
  assert.match(p403FinalizeScript, /Ligamina-p403-evidence-/);
  assert.match(p403FinalizeScript, /rmtree/);
  assert.match(p403FinalizeScript, /runner_temp_bundle_sha256/);
  assert.match(p403SelfTestScript, /runner_contract_success/);
  assert.match(p403SelfTestScript, /runner_contract_failure/);
  assert.match(p403SelfTestScript, /runner_contract_cancel/);
  assert.match(p403SelfTestScript, /plugin_execution.*not_started/);
  assert.match(p403SelfTestScript, /trap 'exit 143' TERM/);
});

test('Linux self-test records failure state and never dispatches third-party plugins', () => {
  assert.match(selfTestScript, /trap finish EXIT/);
  assert.match(selfTestScript, /"windows_tests_run": False/);
  assert.match(selfTestScript, /"plugin_execution": False/);
  assert.doesNotMatch(selfTestScript, /execute-recovery-pilot|run_s205/);
});
