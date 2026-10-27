Set-Location -LiteralPath $PSScriptRoot
python -m whale_friend
if ($LASTEXITCODE -ne 0) { Read-Host '启动失败，按回车关闭' }
