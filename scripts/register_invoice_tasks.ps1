<#
.SYNOPSIS
  Dang ky Windows Task Scheduler chay job lay hoa don (web/invoice_job.py tick).

.DESCRIPTION
  - Chay "tick" vao cac gio co dinh (mac dinh 06:30, 12:30, 19:00).
  - Quet lai thang truoc vao mung 5 va 15: tick tu lam, khong can task rieng.
  - May tat dung gio -> chay bu khi mo may (StartWhenAvailable).
  - Khong chay chong (IgnoreNew + khoa advisory trong PostgreSQL).
  - Mac dinh chay duoi user hien tai va CHI khi user do dang dang nhap. Tren may
    chu khong ai ngoi dang nhap -> dung -RunWhenLoggedOff de task chay ca khi da
    dang xuat (LogonType S4U, khong phai luu mat khau).
  - Dung pythonw.exe neu co (khong bat cua so console).
    Log: output\logs\invoice_job_<yyyy-mm>.log
  - Cau hinh (DATABASE_URL, HDT_PASSWORD_<MST>) doc tu file .env o thu muc goc.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\register_invoice_tasks.ps1
  powershell -ExecutionPolicy Bypass -File scripts\register_invoice_tasks.ps1 -Times 07:00,17:00
  powershell -ExecutionPolicy Bypass -File scripts\register_invoice_tasks.ps1 -RunWhenLoggedOff
  powershell -ExecutionPolicy Bypass -File scripts\register_invoice_tasks.ps1 -Unregister
#>
param(
    [string[]]$Times = @("06:30", "12:30", "19:00"),
    [string]$Python = "",
    [string]$TaskName = "GDT Invoice Job",
    [switch]$RunWhenLoggedOff,
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

$register = @{
    TaskName    = $TaskName
    Action      = $action
    Trigger     = $triggers
    Settings    = $settings
    Description = "Lay hoa don GDT (XML) + PDF ban goc, co hop cho thu lai"
    Force       = $true
}
if ($RunWhenLoggedOff) {
    # S4U = "chay du user co dang nhap hay khong", khong can luu mat khau. Task
    # van chay duoi chinh user nay nen doc duoc .env va thu muc du an.
    $register.Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType S4U -RunLevel Limited
}
Register-ScheduledTask @register | Out-Null

Write-Output "Da dang ky '$TaskName' luc: $($Times -join ', ')"
Write-Output "Python: $Python"
if ($RunWhenLoggedOff) {
    Write-Output "Che do: chay ca khi da dang xuat (S4U) - hop cho may chu"
} else {
    Write-Output "Che do: CHI chay khi '$env:USERNAME' dang dang nhap."
    Write-Output "        Tren may chu, chay lai voi -RunWhenLoggedOff"
}
Write-Output "Chay thu ngay: Start-ScheduledTask -TaskName '$TaskName'"
