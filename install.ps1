# Tomecast installer for Windows: https://github.com/Kingskid2026/tomecast
#
#   irm https://raw.githubusercontent.com/Kingskid2026/tomecast/main/install.ps1 | iex
#
# DRAFT for the Windows version (#39). It isn't linked from anywhere yet, and Tomecast itself
# doesn't run on Windows yet, so the final check below fails until the port is done.
#
# It follows install.sh step by step (no administrator rights; everything stays in your
# user profile):
#   1. Installs uv, the Python tool manager, into ~\.local\bin if you don't have it.
#      uv's installer adds that folder to your PATH.
#   2. Uses uv to install Tomecast from PyPI. uv downloads a suitable Python if needed.
#   3. Runs `tomecast --version` to check that it worked, and makes sure the `tomecast`
#      command will be on your PATH in new terminals.
#   4. (Not yet.) Adds Tomecast to the Start Menu and to the "Open with" menu for EPUB files.
#      install.sh does this with `tomecast desktop-entry install`, which only knows Linux's
#      launcher so far.
#
# Tomecast downloads its voice engines and models later, on first launch, after asking.
#
# Everything is inside one function, because `irm | iex` runs this in your own PowerShell
# session: nothing here calls `exit` (that would close your window) or leaves variables
# behind. Errors are thrown, which stops the script and shows the message.

function Install-Tomecast {
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingWriteHost', '',
        Justification = 'An installer talks to the person running it.')]
    param()

    $UvVersion = '0.13.7'
    # CI points this at a local checkout to test unreleased code.
    $Package = if ($env:TOMECAST_PACKAGE) { $env:TOMECAST_PACKAGE } else { 'tomecast' }

    function Write-Step([string] $Message) { Write-Host "tomecast-install: $Message" }

    if ($env:OS -ne 'Windows_NT') {
        throw 'tomecast-install: error: this installer is for Windows. On Linux, use install.sh.'
    }

    $uvCommand = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue
    if ($uvCommand) {
        $Uv = $uvCommand.Source
        Write-Step "using the uv already on your system ($(& $Uv --version))"
    }
    else {
        Write-Step "installing uv $UvVersion (the tool Tomecast uses to manage Python) into ~\.local\bin"
        # uv's own installer, pinned to a version, in a separate PowerShell so nothing it does
        # can affect this session.
        & powershell -NoProfile -ExecutionPolicy Bypass -Command `
            "irm https://astral.sh/uv/$UvVersion/install.ps1 | iex"
        $Uv = Join-Path $HOME '.local\bin\uv.exe'
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $Uv)) {
            throw 'tomecast-install: error: uv did not install correctly. See https://docs.astral.sh/uv/ for help.'
        }
    }

    # uv remembers PyPI's package list for a few minutes; these flags ask again for Tomecast
    # only, so a release published moments ago is picked up. Older uv versions get the plain
    # command.
    $installed = (& $Uv tool list 2>$null) -match '^tomecast '
    if ($installed) {
        Write-Step 'Tomecast is already installed; upgrading it'
        & $Uv tool upgrade --reinstall-package tomecast tomecast 2>$null
        if ($LASTEXITCODE -ne 0) { & $Uv tool upgrade tomecast }
    }
    else {
        Write-Step 'installing Tomecast'
        & $Uv tool install --refresh-package tomecast $Package 2>$null
        if ($LASTEXITCODE -ne 0) { & $Uv tool install $Package }
    }
    if ($LASTEXITCODE -ne 0) {
        throw 'tomecast-install: error: uv could not install Tomecast (its message is above).'
    }

    $BinDir = (& $Uv tool dir --bin | Select-Object -Last 1).Trim()
    $Tomecast = Join-Path $BinDir 'tomecast.exe'
    $version = & $Tomecast --version 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "tomecast-install: error: Tomecast installed, but didn't start. Please open an issue."
    }
    Write-Step "done! $version is installed."

    # Step 4 (Start Menu and "Open with") goes here once `tomecast desktop-entry install`
    # knows Windows (#39). Like install.sh's, it must never fail the install.

    $onPath = ($env:Path -split ';') | Where-Object { $_.TrimEnd('\') -ieq $BinDir.TrimEnd('\') }
    if ($onPath) {
        Write-Step "run 'tomecast --help' to get started."
    }
    else {
        # Adds uv's tool folder to your user PATH (new terminals see it).
        & $Uv tool update-shell *> $null
        Write-Step "open a new terminal, then run 'tomecast --help' to get started."
    }
}

Install-Tomecast
