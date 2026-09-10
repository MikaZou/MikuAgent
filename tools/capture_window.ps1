# 抓取桌宠窗口的屏幕区域（含子控件合成结果），用于验证透明桌宠的真实观感。
#
# 为什么不用 Process.MainWindowHandle：Qt 的 Qt.Tool 窗口没有 WS_EX_APPWINDOW，
# .NET 经常把它当成"没有主窗口"，导致取到空句柄或抓错窗口。
# 这里改为 EnumWindows 按 PID 列出所有顶层窗口，优先挑可见且尺寸最大的那个。
#
# 用法:
#   powershell -File tools\capture_window.ps1 -Out .tmp\shot.png
#   powershell -File tools\capture_window.ps1 -ProcName "python*" -Out shot.png
param(
    [string]$ProcName = "python*",
    [string]$Out = ".tmp\shot.png",
    [int]$DelaySeconds = 0,
    [switch]$Hover
)

Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

public class WinCap {
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }

    public delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);

    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT r);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
    [DllImport("user32.dll")] public static extern int GetWindowTextLength(IntPtr hWnd);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr lParam);

    public static List<IntPtr> WindowsOfPid(uint target) {
        var found = new List<IntPtr>();
        EnumWindows(delegate(IntPtr h, IntPtr l) {
            uint pid; GetWindowThreadProcessId(h, out pid);
            if (pid == target) found.Add(h);
            return true;
        }, IntPtr.Zero);
        return found;
    }

    public static string TitleOf(IntPtr h) {
        int n = GetWindowTextLength(h);
        var sb = new StringBuilder(n + 2);
        GetWindowText(h, sb, sb.Capacity);
        return sb.ToString();
    }
}
"@

[void][WinCap]::SetProcessDPIAware()
if ($DelaySeconds -gt 0) { Start-Sleep -Seconds $DelaySeconds }

# 收集候选窗口：按 PID 枚举 + 标题匹配
$candidates = @()
foreach ($proc in (Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -like $ProcName })) {
    foreach ($h in [WinCap]::WindowsOfPid([uint32]$proc.Id)) {
        if (-not [WinCap]::IsWindowVisible($h)) { continue }
        $r = New-Object WinCap+RECT
        [void][WinCap]::GetWindowRect($h, [ref]$r)
        $w = $r.Right - $r.Left; $hgt = $r.Bottom - $r.Top
        if ($w -lt 120 -or $hgt -lt 120) { continue }
        $candidates += [pscustomobject]@{
            Handle = $h; Pid = $proc.Id; Proc = $proc.ProcessName
            Title = [WinCap]::TitleOf($h); W = $w; H = $hgt
            Left = $r.Left; Top = $r.Top; Area = $w * $hgt
        }
    }
}

if ($candidates.Count -eq 0) {
    Write-Host "NO WINDOW for process '$ProcName'"
    Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowTitle } |
        ForEach-Object { Write-Host "  candidate: [$($_.MainWindowTitle)] pid=$($_.Id) proc=$($_.ProcessName)" }
    exit 1
}

$win = $candidates | Sort-Object -Property Area -Descending | Select-Object -First 1
Write-Host "picked: pid=$($win.Pid) $($win.Proc) title=[$($win.Title)] $($win.W)x$($win.H) at $($win.Left),$($win.Top)"

[void][WinCap]::SetForegroundWindow($win.Handle)
Start-Sleep -Milliseconds 900

if ($Hover) {
    # 把鼠标放到窗口中心，触发悬停显示输入栏
    [void][WinCap]::SetCursorPos($win.Left + [int]($win.W / 2), $win.Top + [int]($win.H / 2))
    Start-Sleep -Milliseconds 1200
}

$r2 = New-Object WinCap+RECT
[void][WinCap]::GetWindowRect($win.Handle, [ref]$r2)
$w2 = $r2.Right - $r2.Left; $h2 = $r2.Bottom - $r2.Top

$bmp = New-Object System.Drawing.Bitmap($w2, $h2)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($r2.Left, $r2.Top, 0, 0, (New-Object System.Drawing.Size($w2, $h2)))

$full = if ([System.IO.Path]::IsPathRooted($Out)) { $Out } else { Join-Path (Get-Location) $Out }
$dir = Split-Path $full -Parent
if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
$bmp.Save($full, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
Write-Host "saved $full (${w2}x${h2})"
