# 抓取指定窗口的屏幕区域（含子控件合成结果），用于验证透明桌宠的真实观感。
# 用法: powershell -File tools\capture_window.ps1 -TitleMatch "*MikuAgent*" -Out .tmp\shot.png
param(
    [string]$TitleMatch = "*MikuAgent*",
    [string]$Out = ".tmp\shot.png",
    [int]$DelaySeconds = 0
)

Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WinCap {
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
}
"@

[void][WinCap]::SetProcessDPIAware()
if ($DelaySeconds -gt 0) { Start-Sleep -Seconds $DelaySeconds }

$proc = Get-Process -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowTitle -like $TitleMatch } |
        Select-Object -First 1
if (-not $proc) {
    Write-Host "NO WINDOW matching '$TitleMatch'"
    Get-Process -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowTitle } |
        ForEach-Object { Write-Host "  candidate: [$($_.MainWindowTitle)] pid=$($_.Id)" }
    exit 1
}

$h = $proc.MainWindowHandle
[void][WinCap]::SetForegroundWindow($h)
Start-Sleep -Milliseconds 800

$r = New-Object WinCap+RECT
[void][WinCap]::GetWindowRect($h, [ref]$r)
$w = $r.Right - $r.Left
$hgt = $r.Bottom - $r.Top
if ($w -le 0 -or $hgt -le 0) { Write-Host "BAD RECT ${w}x${hgt}"; exit 1 }

$bmp = New-Object System.Drawing.Bitmap($w, $hgt)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($r.Left, $r.Top, 0, 0, (New-Object System.Drawing.Size($w, $hgt)))
$full = Join-Path (Get-Location) $Out
$dir = Split-Path $full -Parent
if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
$bmp.Save($full, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
Write-Host "saved $full (${w}x${hgt}) rect=$($r.Left),$($r.Top)"
