# VicePulse: one subtitled, vertical highlight from a video on this PC. Nothing is published.
#
# Run from the gta-media folder:
#   powershell -ExecutionPolicy Bypass -File scripts\make-highlight.ps1
# or point it straight at a file:
#   powershell -ExecutionPolicy Bypass -File scripts\make-highlight.ps1 -Path "C:\Users\me\Downloads\clip.mp4"
#
# What it does: lists the videos in your Downloads folder (or uses -Path), asks one question about the
# footage's rights, makes sure the worker has local Whisper (free, offline speech-to-text), runs the
# VicePulse pipeline for one 30-60 s English highlight, saves the MP4 in Videos\VicePulse and opens it.
# It does not change .env, delete anything, reset the database or touch Docker volumes.

param(
    [string]$Path = "",
    [int]$Seconds = 45,
    [ValidateSet("en", "br")][string]$Channel = "en",
    [ValidateSet("tiny", "base", "small")][string]$WhisperModel = "small"
)

# Native commands report through $LASTEXITCODE (checked after each step); "Stop" would turn Docker's progress
# messages on stderr into errors in Windows PowerShell 5.1.
$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Set-Location (Split-Path -Parent $PSScriptRoot)

function Stop-With($msg) { Write-Host ""; Write-Host $msg -ForegroundColor Red; exit 1 }
function Clean($text) { return ($text -replace '"', '').Trim() }

# ---- 1. the video -----------------------------------------------------------------------------
if (-not $Path) {
    $downloads = Join-Path $HOME "Downloads"
    $videos = @(Get-ChildItem -Path $downloads -File -Recurse -Depth 1 -ErrorAction SilentlyContinue |
        Where-Object { $_.Extension -match '^\.(mp4|mkv|mov|webm|avi|m4v|flv|ts)$' } |
        Sort-Object LastWriteTime -Descending | Select-Object -First 15)
    if ($videos.Count -eq 0) {
        Write-Host "No video files found in $downloads."
        $Path = Clean (Read-Host "Paste the exact full path of the video file")
    } else {
        Write-Host ""
        Write-Host "Videos in your Downloads folder (newest first):" -ForegroundColor Cyan
        for ($i = 0; $i -lt $videos.Count; $i++) {
            $gb = "{0:N2} GB" -f ($videos[$i].Length / 1GB)
            Write-Host ("  [{0}] {1}   ({2}, {3:yyyy-MM-dd HH:mm})" -f ($i + 1), $videos[$i].Name, $gb, $videos[$i].LastWriteTime)
        }
        $pick = Clean (Read-Host "Type the number of the video (or paste a full path if it isn't listed)")
        if ($pick -match '^\d+$' -and [int]$pick -ge 1 -and [int]$pick -le $videos.Count) {
            $Path = $videos[[int]$pick - 1].FullName
        } else {
            $Path = $pick
        }
    }
}
$Path = Clean $Path
if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { Stop-With "File not found: $Path  - run again and give the exact path." }
$file = Get-Item -LiteralPath $Path
Write-Host "Using: $($file.FullName)" -ForegroundColor Green

# ---- 2. one rights question -------------------------------------------------------------------
Write-Host ""
Write-Host "Where does this footage come from?" -ForegroundColor Cyan
Write-Host "  [1] I recorded it myself (my own gameplay)"
Write-Host "  [2] It has a Creative Commons licence that allows commercial use and edits (CC BY / CC0)"
Write-Host "  [3] The creator gave me written permission to clip, edit and monetise it"
Write-Host "  [4] None of these (for example, downloaded from someone else's stream)"
$answer = Clean (Read-Host "Type 1, 2, 3 or 4")
$rightsArgs = @()
switch ($answer) {
    "1" { $rightsArgs = @("--rights", "own") }
    "2" {
        $lic = Clean (Read-Host "Licence name exactly as shown (for example CC BY 4.0)")
        $author = Clean (Read-Host "Author to credit")
        $url = Clean (Read-Host "Link to the licence or the file page")
        $rightsArgs = @("--rights", "cc", "--licence", $lic, "--author", $author, "--url", $url)
    }
    "3" {
        $creator = Clean (Read-Host "Creator's name")
        $url = Clean (Read-Host "Link to the written permission (message, email screenshot or document)")
        $rightsArgs = @("--rights", "permission", "--creator", $creator, "--url", $url)
    }
    default { Stop-With "Stopped: VicePulse only processes footage you own or are licensed to use. Nothing was changed." }
}

# ---- 3. worker with local Whisper -------------------------------------------------------------
Write-Host ""
Write-Host "Checking the VicePulse worker..." -ForegroundColor Cyan
docker compose up -d vicepulse | Out-Null
docker compose exec -T vicepulse python -c "import faster_whisper" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Adding local Whisper to the worker (one-time build, a few minutes; data and volumes are kept)..."
    $env:VICEPULSE_BUILD_WHISPER = "1"
    docker compose build vicepulse
    if ($LASTEXITCODE -ne 0) { Stop-With "The worker build failed - copy the output above and send it to Claude." }
    docker compose up -d vicepulse
    Start-Sleep -Seconds 5
    docker compose exec -T vicepulse python -c "import faster_whisper"
    if ($LASTEXITCODE -ne 0) { Stop-With "Whisper is still missing in the worker - send the output above to Claude." }
}
Write-Host "Worker ready (Whisper installed)." -ForegroundColor Green

# ---- 4. copy the video into the worker --------------------------------------------------------
$safe = ($file.BaseName -replace '[^A-Za-z0-9._-]+', '_')
if ($safe.Length -gt 80) { $safe = $safe.Substring(0, 80) }
$inside = "/data/incoming/pc/$safe$($file.Extension.ToLower())"
docker compose exec -T vicepulse mkdir -p /data/incoming/pc
Write-Host "Copying the video into the worker ($('{0:N2}' -f ($file.Length / 1GB)) GB)..."
docker compose cp "$($file.FullName)" "vicepulse:$inside"
if ($LASTEXITCODE -ne 0) { Stop-With "Copy failed - send the output above to Claude." }

# ---- 5. run the pipeline ----------------------------------------------------------------------
Write-Host ""
Write-Host "Finding the best $Seconds-second moment, transcribing it and rendering 1080x1920..." -ForegroundColor Cyan
Write-Host "(long videos take a while: the whole file is scanned for scene cuts and loud moments;"
Write-Host " the first run also downloads the Whisper '$WhisperModel' model once)"
$cmd = @("compose", "exec", "-T", "-e", "VICEPULSE_TRANSCRIBER=whisper", "-e", "WHISPER_MODEL=$WhisperModel",
         "-e", "VICEPULSE_CAPTIONS=transcript", "vicepulse",
         "vicepulse", "highlight", "--file", $inside, "--channel", $Channel, "--clip-seconds", "$Seconds") + $rightsArgs
$raw = & docker @cmd
if ($LASTEXITCODE -ne 0) { Stop-With "The pipeline stopped with an error (shown above). Send it to Claude." }
$result = ($raw | Select-Object -Last 1) | ConvertFrom-Json

if ($result.refused) { Stop-With "The rights gate refused the file: $($result.refused -join '; ')" }
if (-not $result.finished_files -or $result.finished_files.Count -eq 0) {
    Write-Host ($raw -join "`n")
    Stop-With "No clip was produced (details above). Send this output to Claude."
}

# ---- 6. save to Videos\VicePulse and open -----------------------------------------------------
$outDir = Join-Path ([Environment]::GetFolderPath("MyVideos")) "VicePulse"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$finished = $result.finished_files[0]
$dest = Join-Path $outDir (Split-Path $finished -Leaf)
docker compose cp "vicepulse:$finished" "$dest"
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $dest)) { Stop-With "Could not copy the finished MP4 out of the worker." }

Write-Host ""
Write-Host "Done. Nothing was published." -ForegroundColor Green
foreach ($d in $result.details) {
    Write-Host ("  clip {0}: {1:N1}s to {2:N1}s of the source, subtitles from {3} ({4} lines), status {5}" -f `
        $d.clip, $d.from_s, $d.to_s, $d.subtitles, $d.subtitle_lines, $d.status)
}
if ($result.already_done) { Write-Host "  (this file was processed before, so the existing clip was reused)" }
Write-Host "  Saved: $dest"
Start-Process -FilePath $dest
