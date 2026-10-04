# Verify the settings window's quit button by ACTUALLY CLICKING IT via UI Automation.
#
# Why this file exists: the quit path has several triggers (pet corner X, tray menu,
# settings button). They all funnel into ui/app_control.py::quit_all, but "the button
# is wired up" is an assumption worth testing rather than asserting. This drives the
# real button in the real window - no synthetic shortcut.
#
# Why UIA and not mouse_event: moving the physical cursor would fight the user for
# their mouse. UIA invokes the control directly.
#
# THIS FILE MUST STAY ASCII-ONLY. The UI labels are Chinese, so they are built from
# code points instead of being written literally: this repo's scripts are UTF-8 with
# LF endings and no BOM, and PowerShell 5.1 decodes those byte pairs wrongly, which
# eats newlines and breaks parsing (measured - it reported "missing }" and mangled
# the very string literals in this file). See tools/desktop_probe.ps1 for the same note.
param(
    [string]$WindowTitle = "MikuAgent",
    [int]$TimeoutSec = 25
)

Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes

# "完全退出" = U+5B8C U+5168 U+9000 U+51FA
$quitCn = [string][char]0x5B8C + [char]0x5168 + [char]0x9000 + [char]0x51FA
$settingsButton = "$quitCn MikuAgent"

function Find-Window {
    param([string]$Needle)
    $root = [System.Windows.Automation.AutomationElement]::RootElement
    $all = $root.FindAll([System.Windows.Automation.TreeScope]::Children,
                         [System.Windows.Automation.Condition]::TrueCondition)
    foreach ($w in $all) {
        if ($w.Current.Name -like "*$Needle*") { return $w }
    }
    return $null
}

# Qt reports a QMessageBox as a child of its parent window rather than as a separate
# top-level UIA child, so searching top-level windows for the dialog finds nothing
# (measured: the dialog was open, its buttons were listed under the settings window).
# Search all windows of the app's process instead.
function Find-ButtonEverywhere {
    param([int]$ProcessId, [string]$Name)
    $root = [System.Windows.Automation.AutomationElement]::RootElement
    $all = $root.FindAll([System.Windows.Automation.TreeScope]::Children,
                         [System.Windows.Automation.Condition]::TrueCondition)
    $buttonCond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Button)
    foreach ($w in $all) {
        if ($w.Current.ProcessId -ne $ProcessId) { continue }
        $buttons = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants, $buttonCond)
        foreach ($b in $buttons) {
            if ($b.Current.Name -eq $Name) { return $b }
        }
    }
    return $null
}

function Invoke-Element {
    param($Element)
    $pattern = $Element.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
    $pattern.Invoke()
}

function Invoke-Button {
    param($Window, [string]$Name)
    $cond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Button)
    $buttons = $Window.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)
    foreach ($b in $buttons) {
        if ($b.Current.Name -eq $Name) {
            Invoke-Element $b
            return $true
        }
    }
    Write-Output "  (buttons seen: $((($buttons | ForEach-Object { $_.Current.Name }) -join ' | ')))"
    return $false
}

$appWindow = Find-Window $WindowTitle
if ($null -eq $appWindow) {
    Write-Output "FAIL: no top-level window matching '$WindowTitle' - is the app running?"
    exit 2
}
$appPid = $appWindow.Current.ProcessId
Write-Output "app pid=$appPid"

# Find the button by NAME rather than by picking a window first: the app has two
# top-level windows (the frameless pet and the settings window) and their z-order
# in the UIA child list changes, so "first window whose name matches" sometimes
# returns the pet window - which has no quit button (measured).
$quitButton = $null
$tries = 0
while ($tries -lt 20) {
    $tries++
    $quitButton = Find-ButtonEverywhere -ProcessId $appPid -Name $settingsButton
    if ($null -ne $quitButton) { break }
    Start-Sleep -Milliseconds 250
}
if ($null -eq $quitButton) {
    Write-Output "FAIL: no '$settingsButton' button found in any app window"
    exit 3
}
Invoke-Element $quitButton
Write-Output "clicked the settings quit button"

# The confirmation box is owned by the settings window; poll for its button.
$confirmButton = $null
$tries = 0
while ($tries -lt 24) {
    Start-Sleep -Milliseconds 250
    $tries++
    $confirmButton = Find-ButtonEverywhere -ProcessId $appPid -Name $quitCn
    if ($null -ne $confirmButton) { break }
}
if ($null -eq $confirmButton) {
    Write-Output "FAIL: confirmation button '$quitCn' did not appear"
    exit 4
}
Write-Output "confirmation dialog is up"

Invoke-Element $confirmButton
Write-Output "confirmed; waiting for the process tree to disappear..."

$deadline = (Get-Date).AddSeconds($TimeoutSec)
$left = @()
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 400
    $left = @(Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
              Where-Object { $_.CommandLine -like '*main.py*' })
    if ($left.Count -eq 0) { break }
}
if ($left.Count -ne 0) {
    Write-Output "FAIL: still alive after ${TimeoutSec}s: $($left.ProcessId -join ',')"
    exit 6
}
Write-Output "OK: all MikuAgent python processes exited"

$busy = @()
foreach ($port in 8765, 18520) {
    if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
        $busy += $port
    }
}
if ($busy.Count -ne 0) {
    Write-Output "FAIL: port(s) still listening: $($busy -join ',')"
    exit 7
}
Write-Output "OK: ports 8765 / 18520 released"
exit 0
