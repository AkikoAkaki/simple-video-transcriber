# simple-video-transcriber — setup_context_menu.ps1
# Adds "转录" to the Explorer right-click menu of audio and video files (HKCU, no admin rights).
# On Windows 11 it appears under "Show more options". Run once with:
#   powershell -ExecutionPolicy Bypass -File setup_context_menu.ps1
# Remove it with -Uninstall.

param([switch]$Uninstall)

$ErrorActionPreference = "Stop"

# .ts is left out: on a developer machine it is far more often TypeScript than a video stream.
$extensions = @(".mp4", ".m4a", ".mov", ".mkv", ".webm", ".mp3", ".wav", ".flac", ".aac", ".opus")
$verb = "MeetingTranscriber"

if ($Uninstall) {
    foreach ($ext in $extensions) {
        Remove-Item -Path "HKCU:\Software\Classes\SystemFileAssociations\$ext\shell\$verb" -Recurse -ErrorAction SilentlyContinue
    }
    Write-Host "[OK] Removed the Explorer menu entry."
    exit 0
}

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$pythonExe = if (Test-Path $venvPython) { $venvPython } else { python -c "import sys; print(sys.executable)" }
$pythonw = $pythonExe -replace "python\.exe$", "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $pythonExe }
$script = Join-Path $PSScriptRoot "enqueue.py"
$command = "`"$pythonw`" `"$script`" `"%1`""

foreach ($ext in $extensions) {
    $key = "HKCU:\Software\Classes\SystemFileAssociations\$ext\shell\$verb"
    New-Item -Path "$key\command" -Force | Out-Null
    Set-ItemProperty -Path $key -Name "MUIVerb" -Value "转录"
    Set-ItemProperty -Path $key -Name "Icon" -Value "$pythonw,0"
    # One enqueue process per selected file; the tray merges them into its queue.
    Set-ItemProperty -Path $key -Name "MultiSelectModel" -Value "Document"
    Set-ItemProperty -Path "$key\command" -Name "(default)" -Value $command
}
Write-Host "[OK] Added '转录' to the right-click menu of $($extensions -join ', ')"
Write-Host "     Command: $command"
