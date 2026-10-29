Set-Location -LiteralPath $PSScriptRoot
$taskLauncher = Join-Path $PSScriptRoot 'launch.pyw'
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
$taskArguments = @('"' + $taskLauncher + '"')
if (-not (Test-Path -LiteralPath $taskPython)) {
    $taskCommand = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($taskCommand) {
        $taskPython = $taskCommand.Source
    } else {
        $taskCommand = Get-Command pyw.exe -ErrorAction SilentlyContinue
        if ($taskCommand) {
            $taskPython = $taskCommand.Source
            $taskArguments = @('-3', ('"' + $taskLauncher + '"'))
        } else {
            Write-Error 'Python 3.11+ with Tcl/Tk support was not found.'
            Read-Host 'Press Enter to close'
            exit 1
        }
    }
}
try {
    Start-Process -FilePath $taskPython -ArgumentList $taskArguments -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -ErrorAction Stop | Out-Null
} catch {
    Write-Error 'Could not start Whale Friend. See README.md or .local/startup.log.'
    Read-Host 'Press Enter to close'
    exit 1
}
