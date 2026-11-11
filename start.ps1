$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
$taskCondaCommand = Get-Command conda.exe -ErrorAction SilentlyContinue
$taskCondaCandidates = @(
    $env:CONDA_EXE
    $(if ($taskCondaCommand) { $taskCondaCommand.Source })
    (Join-Path $env:USERPROFILE 'miniconda3\Scripts\conda.exe')
    (Join-Path $env:USERPROFILE 'anaconda3\Scripts\conda.exe')
)
$taskConda = $taskCondaCandidates | Where-Object {
    $_ -and (Test-Path -LiteralPath $_ -PathType Leaf)
} | Select-Object -First 1
if (-not $taskConda) {
    Write-Host '未找到 Conda。请先在 Conda 终端运行：conda env create --file environment.yml'
    Write-Host '然后运行：conda activate srs-service; python main.py'
    exit 1
}
& $taskConda run --no-capture-output --name srs-service python (Join-Path $PSScriptRoot 'main.py')
exit $LASTEXITCODE
