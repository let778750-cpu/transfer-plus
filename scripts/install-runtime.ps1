param([string]$RuntimeRoot = '', [switch]$NoRegister)
$ErrorActionPreference = 'Stop'
if (-not $RuntimeRoot) {
    $dataDrive = Get-PSDrive -Name D -ErrorAction SilentlyContinue
    $base = if ($dataDrive) { Join-Path $dataDrive.Root 'ZoteroTools' } else { $env:LOCALAPPDATA }
    $RuntimeRoot = Join-Path $base 'research-max-bilingual'
}
$RuntimeRoot = [IO.Path]::GetFullPath($RuntimeRoot)
$packageRoot = $PSScriptRoot
$manifest = Get-Content -LiteralPath (Join-Path $packageRoot 'runtime-manifest.json') -Raw | ConvertFrom-Json
foreach ($entry in $manifest.files) {
    $file = [IO.Path]::GetFullPath((Join-Path $packageRoot $entry.path))
    if (-not $file.StartsWith($packageRoot.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid package path' }
    if ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLower() -ne $entry.sha256) { throw "Package checksum mismatch: $($entry.path)" }
}
$versionName = $manifest.version
if ($versionName -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid runtime version' }
$versionRoot = Join-Path (Join-Path $RuntimeRoot 'versions') $versionName
$incoming = Join-Path (Join-Path $packageRoot 'versions') $versionName
# Version directories are immutable. Reinstalling identical contents is safe;
# replacing an existing build under the same version would destroy rollback.
if (Test-Path -LiteralPath $versionRoot) {
    $prefix = "versions/$versionName/"
    foreach ($entry in $manifest.files) {
        if (-not $entry.path.StartsWith($prefix, [StringComparison]::Ordinal)) { continue }
        $installedFile = Join-Path $versionRoot $entry.path.Substring($prefix.Length)
        if (-not (Test-Path -LiteralPath $installedFile -PathType Leaf) -or
            (Get-FileHash -LiteralPath $installedFile -Algorithm SHA256).Hash.ToLower() -ne $entry.sha256) {
            throw "Existing runtime $versionName differs. Keep it for rollback; install a new version or choose a separate RuntimeRoot."
        }
    }
}
New-Item -ItemType Directory -Path $versionRoot -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $incoming 'server') -Destination $versionRoot -Recurse -Force
foreach ($folder in @('python','licenses','resources')) {
    Copy-Item -LiteralPath (Join-Path $incoming $folder) -Destination $versionRoot -Recurse -Force
}
$dataRoot = Join-Path $RuntimeRoot 'data'
foreach ($folder in @('config','glossary','cache','jobs','reports','logs')) {
    New-Item -ItemType Directory -Path (Join-Path $dataRoot $folder) -Force | Out-Null
}
$utf8 = New-Object System.Text.UTF8Encoding($false)
& (Join-Path $versionRoot 'python\python.exe') -I -B (Join-Path $versionRoot 'server\diagnose.py') --data-dir $dataRoot
if ($LASTEXITCODE -ne 0) { throw 'Runtime diagnostics failed; active version was not changed' }
$activePath = Join-Path $RuntimeRoot 'active.json'
$active = @{version=$versionName}
if (Test-Path -LiteralPath $activePath) {
    $previous = Get-Content -LiteralPath $activePath -Raw | ConvertFrom-Json
    if ($previous.version -ne $versionName) { $active.previousVersion=$previous.version }
    elseif ($previous.previousVersion) { $active.previousVersion=$previous.previousVersion }
}
[IO.File]::WriteAllText($activePath, ($active|ConvertTo-Json), $utf8)
if (-not $NoRegister) {
$registry = Join-Path $env:LOCALAPPDATA 'ResearchMaxBilingual'
New-Item -ItemType Directory -Path $registry -Force | Out-Null
[IO.File]::WriteAllText((Join-Path $registry 'install.json'), (@{root=$RuntimeRoot;data=$dataRoot}|ConvertTo-Json), $utf8)
}
Write-Host "Runtime installed: $versionRoot"
Write-Host 'Install the matching XPI using Zotero Tools > Plugins > gear > Install Plugin From File.'
