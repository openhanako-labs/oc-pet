$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

# ── Python ──
Write-Host "[1/4] 安装 Python..."
winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements --silent 2>$null
$env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")
$py = (Get-Command python).Source

# ── 源码 ──
Write-Host "[2/4] 下载源码..."
irm "https://github.com/openhanako-labs/oc-pet/archive/refs/heads/main.zip" -OutFile "$env:TEMP\oc-pet.zip"
Expand-Archive "$env:TEMP\oc-pet.zip" "$env:TEMP\oc-pet-extract" -Force
Move-Item "$env:TEMP\oc-pet-extract\oc-pet-main" "C:\oc-pet" -Force
Set-Location C:\oc-pet

# ── 依赖 ──
Write-Host "[3/4] 安装依赖..."
& $py -m pip install -r requirements.txt --quiet --disable-pip-version-check

# ── 启动 ──
Write-Host "[4/4] 启动桌宠..."
& $py main.py
