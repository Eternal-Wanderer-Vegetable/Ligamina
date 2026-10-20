import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const root = new URL('../', import.meta.url);
const selfTest = await readFile(new URL('.github/workflows/phase3-linux-self-test.yml', root), 'utf8');
const controlled = await readFile(new URL('.github/workflows/phase3-linux-controlled.yml', root), 'utf8');
const selfTestScript = await readFile(new URL('scripts/phase3-linux-self-test.sh', root), 'utf8');
const controlledScript = await readFile(new URL('scripts/phase3-linux-controlled-runner.sh', root), 'utf8');

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

test('Linux self-test records failure state and never dispatches third-party plugins', () => {
  assert.match(selfTestScript, /trap finish EXIT/);
  assert.match(selfTestScript, /"windows_tests_run": False/);
  assert.match(selfTestScript, /"plugin_execution": False/);
  assert.doesNotMatch(selfTestScript, /execute-recovery-pilot|run_s205/);
});
