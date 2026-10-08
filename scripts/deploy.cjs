const path = require('node:path');
const { spawnSync } = require('node:child_process');

if (process.platform !== 'win32') {
  const python = process.env.PYTHON || 'python';
  const result = spawnSync(python, ['-m', 'poetry', 'run', 'python',
    path.join(__dirname, 'deploy.py'), ...process.argv.slice(2)], { stdio: 'inherit' });
  if (result.error) console.error(result.error.message);
  process.exit(result.status ?? 1);
}
const powershell = path.join(process.env.SystemRoot || 'C:\\Windows',
  'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
const result = spawnSync(powershell, ['-NoProfile', '-ExecutionPolicy', 'Bypass',
  '-File', path.join(__dirname, 'deploy.ps1'), ...process.argv.slice(2)], { stdio: 'inherit' });
if (result.error) console.error(result.error.message);
process.exit(result.status ?? 1);
