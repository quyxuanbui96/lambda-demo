const path = require('node:path');
const { spawnSync } = require('node:child_process');

if (process.platform !== 'win32') {
  console.error('This deployment runner uses Windows PowerShell. See README for other systems.');
  process.exit(1);
}
const powershell = path.join(process.env.SystemRoot || 'C:\\Windows',
  'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
const result = spawnSync(powershell, ['-NoProfile', '-ExecutionPolicy', 'Bypass',
  '-File', path.join(__dirname, 'deploy.ps1'), ...process.argv.slice(2)], { stdio: 'inherit' });
if (result.error) console.error(result.error.message);
process.exit(result.status ?? 1);
