param(
    [ValidateSet('package', 'deploy', 'info')]
    [string]$Task = 'deploy',
    [ValidatePattern('^[a-zA-Z0-9-]+$')]
    [string]$Stage = 'dev',
    [ValidateSet('serverless.yml', 'serverless.workflow.yml')]
    [string]$Config = 'serverless.yml'
)

$ErrorActionPreference = 'Stop'
if ($Stage -eq 'local') {
    throw "Stage 'local' is reserved for serverless-offline."
}

$taskProjectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $taskProjectRoot
try {
    # The requirements plugin invokes the poetry executable directly.
    $taskPythonScripts = & python -c "import sysconfig; print(sysconfig.get_path('scripts'))"
    if ($LASTEXITCODE -ne 0) { throw 'Python is not available.' }
    $env:PATH = "$taskPythonScripts$([IO.Path]::PathSeparator)$env:PATH"

    # Parse dotenv through Python; do not execute .env contents as shell code.
    $taskEnvironmentJson = & python -m poetry run python -c "from scripts.local_env import load_local_environment; import os,json; load_local_environment(); print(json.dumps({k:v for k,v in os.environ.items() if k.startswith(('AWS_', 'CSV_', 'WF_', 'SERVERLESS_', 'POWERTOOLS_')) or k in ('LOG_LEVEL','PRESIGNED_URL_EXPIRES_SECONDS')}))"
    if ($LASTEXITCODE -ne 0) { throw 'Unable to load project environment. Run yarn install:all.' }
    $taskEnvironment = $taskEnvironmentJson | ConvertFrom-Json
    foreach ($taskProperty in $taskEnvironment.PSObject.Properties) {
        [Environment]::SetEnvironmentVariable($taskProperty.Name, [string]$taskProperty.Value, 'Process')
    }

    if ($env:AWS_CA_BUNDLE) {
        if (-not (Test-Path -LiteralPath $env:AWS_CA_BUNDLE)) { throw 'AWS_CA_BUNDLE file not found.' }
        $env:NODE_EXTRA_CA_CERTS = $env:AWS_CA_BUNDLE
        $env:PIP_CERT = $env:AWS_CA_BUNDLE
    }
    $taskRegion = if ($env:AWS_REGION) { $env:AWS_REGION } else { 'ap-southeast-2' }
    $env:DOCKER_DEFAULT_PLATFORM = 'linux/amd64'
    if ($Config -eq 'serverless.workflow.yml' -and $Task -eq 'deploy') {
        $env:STAGE = $Stage
        & python -m poetry run python -c "from src.workflow.config import load_workflow_settings; load_workflow_settings()"
        if ($LASTEXITCODE -ne 0) { throw 'Workflow configuration is incomplete.' }
        if (-not $env:WF_VPC_SUBNET_IDS -or -not $env:WF_VPC_SECURITY_GROUP_IDS) { throw 'Configure workflow private-RDS VPC first.' }
    }
    $taskServerless = Join-Path $taskProjectRoot 'node_modules\serverless\run.js'
    if (-not (Test-Path -LiteralPath $taskServerless)) { throw 'Run yarn install first.' }

    if ($Task -eq 'info') {
        & node $taskServerless info --config $Config --stage $Stage --region $taskRegion
        if ($LASTEXITCODE -ne 0) { throw 'Unable to read deployed service information.' }
        return
    }

    & python -m poetry export --help *> $null
    if ($LASTEXITCODE -ne 0) { throw 'Poetry export plugin missing. Run yarn install:all.' }
    & docker version --format '{{.Server.Version}}'
    if ($LASTEXITCODE -ne 0) { throw 'Start Docker Desktop with the Linux engine before packaging.' }

    & node $taskServerless package --config $Config --stage $Stage --region $taskRegion
    if ($LASTEXITCODE -ne 0) { throw 'Packaging failed; deployment was not started.' }

    if ($Task -eq 'deploy') {
        & node $taskServerless deploy --config $Config --package .serverless --stage $Stage --region $taskRegion
        if ($LASTEXITCODE -ne 0) { throw 'AWS deployment failed. Check CloudFormation stack events.' }
    }
} finally {
    Pop-Location
}
