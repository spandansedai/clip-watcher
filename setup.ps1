# clip-watcher setup for Windows.
# Finds or installs Python 3.10-3.13 and ffmpeg, then installs the Python libraries into .venv.
# Asks before installing anything. Run via setup.bat (or: powershell -ExecutionPolicy Bypass -File setup.ps1)
#   -Yes       install missing apps without asking
#   -NoWinget  don't use winget (download from python.org / gyan.dev instead)
param([switch]$Yes, [switch]$NoWinget)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # makes Invoke-WebRequest much faster
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Root  = $PSScriptRoot
$Tools = Join-Path $Root 'tools'
$Venv  = Join-Path $Root '.venv'
$PythonVersion = '3.12.10'

function Say($msg, $color = 'Gray') { Write-Host $msg -ForegroundColor $color }
function Ask($question) {
    if ($Yes) { return $true }
    $a = Read-Host "$question [Y/n]"
    return ($a -eq '' -or $a -match '^[yY]')
}
function Has-Winget { (-not $NoWinget) -and [bool](Get-Command winget -ErrorAction SilentlyContinue) }

# ------------------------------------------------------------------ Python
function Find-Python {
    $candidates = New-Object System.Collections.Generic.List[object]
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($v in '3.12', '3.13', '3.11', '3.10') { $candidates.Add(@('py', "-$v")) }
    }
    foreach ($name in 'python', 'python3') {
        $c = Get-Command $name -ErrorAction SilentlyContinue
        # skip the Microsoft Store placeholder in WindowsApps - it isn't a real Python
        if ($c -and $c.Source -notlike '*\WindowsApps\*') { $candidates.Add(@($c.Source)) }
    }
    foreach ($dir in @("$env:LOCALAPPDATA\Programs\Python", "$env:ProgramFiles")) {
        Get-ChildItem $dir -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending |
            ForEach-Object { $exe = Join-Path $_.FullName 'python.exe'; if (Test-Path $exe) { $candidates.Add(@($exe)) } }
    }
    foreach ($cand in $candidates) {
        $exe = $cand[0]; $rest = @($cand | Select-Object -Skip 1)
        try {
            $out = & $exe @rest -c "import sys; v=sys.version_info; print(sys.executable if (3,10) <= v[:2] <= (3,13) else '')" 2>$null
            if ($LASTEXITCODE -eq 0 -and $out -and (Test-Path $out.Trim())) { return $out.Trim() }
        } catch { }
    }
    return $null
}

function Install-Python {
    if (Has-Winget) {
        Say "Installing Python $PythonVersion with winget..."
        winget install --id Python.Python.3.12 -e --scope user --silent --accept-source-agreements --accept-package-agreements | Out-Host
    } else {
        $url = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe"
        $exe = Join-Path $env:TEMP "python-$PythonVersion-amd64.exe"
        Say "Downloading Python $PythonVersion from python.org..."
        Invoke-WebRequest $url -OutFile $exe -UseBasicParsing
        Say "Installing Python (just for this user, no admin needed)..."
        Start-Process $exe -ArgumentList '/quiet', 'InstallAllUsers=0', 'Include_launcher=1', 'Include_test=0', 'PrependPath=1' -Wait
        Remove-Item $exe -ErrorAction SilentlyContinue
    }
}

# ------------------------------------------------------------------ ffmpeg
function Find-Ffmpeg {
    $local = Join-Path $Tools 'ffmpeg\bin\ffmpeg.exe'
    if (Test-Path $local) { return $local }
    $c = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    $link = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\ffmpeg.exe'
    if (Test-Path $link) { return $link }
    return $null
}

function Install-Ffmpeg {
    if (Has-Winget) {
        Say "Installing ffmpeg with winget..."
        winget install --id Gyan.FFmpeg -e --silent --accept-source-agreements --accept-package-agreements | Out-Host
        if (Find-Ffmpeg) { return }
        Say "winget didn't work - downloading a portable copy instead." Yellow
    }
    $url = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'
    $zip = Join-Path $env:TEMP 'ffmpeg-release-essentials.zip'
    $tmp = Join-Path $env:TEMP 'clipwatcher-ffmpeg'
    Say "Downloading ffmpeg (about 100 MB) into $Tools\ffmpeg ..."
    Invoke-WebRequest $url -OutFile $zip -UseBasicParsing
    if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
    Expand-Archive $zip -DestinationPath $tmp -Force
    $inner = Get-ChildItem $tmp -Directory | Select-Object -First 1
    New-Item -ItemType Directory -Force $Tools | Out-Null
    $dest = Join-Path $Tools 'ffmpeg'
    if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
    Move-Item $inner.FullName $dest
    Remove-Item $zip, $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

# ------------------------------------------------------------------ main
Say "`n=== clip-watcher setup ===`n" Cyan

$py = Find-Python
if ($py) {
    Say "[ok] Python: $py" Green
} else {
    Say "Python 3.10-3.13 was not found." Yellow
    if (-not (Ask "Install Python $PythonVersion now?")) {
        Say "Please install Python 3.12 from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run setup again." Red
        exit 1
    }
    Install-Python
    $py = Find-Python
    if (-not $py) {
        Say "Python was installed but can't be found yet. Close this window, open a new one and run setup again." Red
        exit 1
    }
    Say "[ok] Python: $py" Green
}

$ff = Find-Ffmpeg
if ($ff) {
    Say "[ok] ffmpeg: $ff" Green
} else {
    Say "ffmpeg was not found." Yellow
    if (-not (Ask "Install ffmpeg now?")) {
        Say "Please install ffmpeg (https://www.gyan.dev/ffmpeg/builds/ - put the 'bin' folder on PATH, or extract it to $Tools\ffmpeg), then run setup again." Red
        exit 1
    }
    Install-Ffmpeg
    $ff = Find-Ffmpeg
    if (-not $ff) { Say "ffmpeg install failed - see the messages above." Red; exit 1 }
    Say "[ok] ffmpeg: $ff" Green
}

$vpy = Join-Path $Venv 'Scripts\python.exe'
if (-not (Test-Path $vpy)) {
    Say "Creating the private Python environment (.venv)..."
    & $py -m venv $Venv
    if ($LASTEXITCODE -ne 0) { Say "Could not create .venv" Red; exit 1 }
}
Say "Installing Python libraries (first time takes a few minutes)..."
& $vpy -m pip install --disable-pip-version-check -q --upgrade pip
& $vpy -m pip install --disable-pip-version-check -q -r (Join-Path $Root 'requirements.txt')
if ($LASTEXITCODE -ne 0) { Say "Library install failed - see the messages above. Check your internet connection and run setup again." Red; exit 1 }
$ErrorActionPreference = 'Continue'
& $vpy -c "import streamlink, faster_whisper, cv2, numpy"
if ($LASTEXITCODE -ne 0) { Say "Libraries installed but failed to load - see the messages above." Red; exit 1 }
Say "[ok] Python libraries" Green

Say "`nSetup complete. Try:  clip <twitch channel> --vertical --burn`n" Cyan
