<#
.SYNOPSIS
  Dang ky Windows Task Scheduler chay job lay hoa don (web/invoice_job.py tick).

.DESCRIPTION
  - Chay "tick" vao cac gio co dinh (mac dinh 06:30, 12:30, 19:00).
  - Quet lai thang truoc vao mung 5 va 15: tick tu lam, khong can task rieng.
  - May tat dung gio -> chay bu khi mo may (StartWhenAvailable).
  - Khong chay chong (IgnoreNew + khoa advisory trong PostgreSQL).
  - Chay duoi user hien tai, khi user dang dang nhap. Dung pythonw.exe neu co
    (khong bat cua so console). Log: output\logs\invoice_job_<yyyy-mm>.log
  - Cau hinh (DATABASE_URL, HDT_PASSWORD_<MST>) doc tu file .env o thu muc goc.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\register_invoice_tasks.ps1
  powershell -ExecutionPolicy Bypass -File scripts\register_invoice_tasks.ps1 -Times 07:00,17:00
  powershell -ExecutionPolicy Bypass -File scripts\register_invoice_tasks.ps1 -Unregister
#>
param(
    [string[]]$Times = @("06:30", "12:30", "19:00"),
    [string]$Python = "",
    [string]$TaskName = "GDT Invoice Job",
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "Da xoa task '$TaskName'"
    return
}

$root = Split-Path -Parent $PSScriptRoot
$job = Join-Path $root "web\invoice_job.py"
if (-not (Test-Path $job)) { throw "Khong thay $job" }
if (-not (Test-Path (Join-Path $root ".env"))) {
    Write-Warning "Chua co file .env o $root (copy tu .env.example) - job se bao thieu DATABASE_URL"
}

if (-not $Python) {
    $py = (Get-Command python -ErrorAction Stop).Source
    $pyw = Join-Path (Split-Path -Parent $py) "pythonw.exe"
    $Python = if (Test-Path $pyw) { $pyw } else { $py }
}

$action = New-ScheduledTaskAction -Execute $Python -Argument "`"$job`" tick" -WorkingDirectory $root
$triggers = foreach ($t in $Times) { New-ScheduledTaskTrigger -Daily -At $t }
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings `
    -Description "Lay hoa don GDT (XML) + PDF ban goc, co hop cho thu lai" -Force | Out-Null

Write-Output "Da dang ky '$TaskName' luc: $($Times -join ', ')"
Write-Output "Python: $Python"
Write-Output "Chay thu ngay: Start-ScheduledTask -TaskName '$TaskName'"
