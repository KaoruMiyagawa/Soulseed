param(
    [string] $Python = (Join-Path $PSScriptRoot '.venv/Scripts/python.exe')
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
$root = $PSScriptRoot
$build = Join-Path $root 'desktop-build'
$frontend = Join-Path $root 'frontend'
$target = 'x86_64-pc-windows-msvc'
$config = Get-Content -Raw -LiteralPath (Join-Path $frontend 'src-tauri/tauri.conf.json') | ConvertFrom-Json

Push-Location $root
try {
    & $Python -c "import sys, struct; assert sys.platform == 'win32' and struct.calcsize('P') == 8, 'Windows x64 Python required'"
    & $Python -m PyInstaller --clean --noconfirm --onedir --console --name soulseed-backend `
        --paths backend --add-data "$(Join-Path $root 'backend/app/persona_profile.md');app" --collect-data tzdata `
        --hidden-import uvicorn.loops.asyncio --hidden-import uvicorn.protocols.http.h11_impl `
        --hidden-import uvicorn.lifespan.on --exclude-module pytest `
        --distpath (Join-Path $build 'sidecar') --workpath (Join-Path $build 'pyinstaller') `
        --specpath $build backend/scripts/desktop_sidecar.py

    Push-Location $frontend
    try {
        npm.cmd run build -- --mode desktop --outDir dist-desktop
        & .\node_modules\.bin\tauri.cmd build --ci --target $target --bundles nsis
    } finally {
        Pop-Location
    }

    $cargoTarget = if ($env:CARGO_TARGET_DIR) {
        [IO.Path]::GetFullPath($env:CARGO_TARGET_DIR, (Join-Path $frontend 'src-tauri'))
    } else {
        Join-Path $frontend 'src-tauri/target'
    }
    $name = "Soulseed_$($config.version)_x64-setup.exe"
    $bundleDir = Join-Path $cargoTarget "$target/release/bundle/nsis"
    $release = Join-Path $build "release/$($config.version)"
    New-Item -ItemType Directory -Path $release -Force | Out-Null
    $installer = Join-Path $release $name
    Copy-Item -LiteralPath (Join-Path $bundleDir $name) -Destination $installer -Force
    $digest = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant()
    Set-Content -LiteralPath (Join-Path $release 'SHA256SUMS.txt') -Value "$digest  $name" -Encoding ascii
    Copy-Item -LiteralPath (Join-Path $root 'docs/windows-install.md') -Destination (Join-Path $release '安装与模型配置.md') -Force
    Copy-Item -LiteralPath (Join-Path $root 'docs/windows-release-notes.md') -Destination (Join-Path $release 'RELEASE-NOTES.md') -Force
    Write-Output "$digest  $installer"
} finally {
    Pop-Location
}
