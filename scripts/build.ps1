# 打包 Windows exe。用法（在仓库根目录）：
#   powershell -ExecutionPolicy Bypass -File scripts\build.ps1
# 需要：Python 3.10+ 在 PATH 里、pip install pyinstaller
# 产物：dist\DSH技能转换器.exe

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    Write-Host "==> PyInstaller 打包" -ForegroundColor Cyan
    python -m PyInstaller `
        --onefile `
        --console `
        --noconfirm `
        --name "DSH技能转换器" `
        --icon "$root\assets\icon.ico" `
        --version-file "$root\scripts\version.txt" `
        --distpath "dist" `
        --workpath ".build" `
        --specpath ".build" `
        "skill-converter\convert.py"

    $exe = Join-Path $root "dist\DSH技能转换器.exe"
    $size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    $hash = (Get-FileHash $exe -Algorithm SHA256).Hash
    Write-Host ""
    Write-Host "==> 完成：$exe（$size MB）" -ForegroundColor Green
    Write-Host "    SHA256: $hash"
} finally {
    Pop-Location
}
