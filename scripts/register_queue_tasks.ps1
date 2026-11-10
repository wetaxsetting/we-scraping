<#
.SYNOPSIS
  Dang ky Task Scheduler cho mo hinh hang doi (thay cho 1 task "tick" duy nhat).

.DESCRIPTION
  Tao 6 task, ten bat dau bang -Prefix (mac dinh "Invoice"):
    <Prefix> Sync Lists        invoice_job.py sync-lists        06:00, 14:00, 22:00 (doi bang -ListTimes)
    <Prefix> Mail              invoice_job.py mail              moi -MailEveryMinutes phut (mac dinh 30)
    <Prefix> Worker XML        invoice_job.py worker --type xml chay mai, tu khoi dong lai khi chet
    <Prefix> Worker PDF        invoice_job.py worker --type pdf chay mai, tu khoi dong lai khi chet
    <Prefix> WeInvoice API     internal_api.py                  chay mai: backend WeInvoice goi de dang nhap
                                                                thu GDT khi them MST (cong SCRAPING_INTERNAL_PORT)
    <Prefix> WeInvoice Worker  weinvoice_worker.py              chay mai: yeu cau SYNC / FETCH_XML / FETCH_PDF /
                                                                IMPORT / TEST_DATA tu weinvoice.winv_engn_rqst

  2 task WeInvoice can .env: SCRAPING_INTERNAL_API_KEY (= WECMS_WEINVOICE_SCRAPING_API_KEY cua backend),
  WEINVOICE_SECRET_KEY (= WECMS_WEINVOICE_SECRET_KEY), WEINVOICE_STORAGE_ROOT (= WECMS_WEINVOICE_STORAGE_ROOT).
  May khong chay WeInvoice: them -NoWeInvoice.

  Worker / API khong co gioi han thoi gian chay, khoi dong cung may (-RunWhenLoggedOff) hoac khi
  dang nhap (mac dinh), va duoc Task Scheduler bat lai sau 1 phut neu thoat voi loi.
  Sau khi dang ky, script bat ngay cac task chay mai (bo qua bang -NoStart).

  Tren may chu BAT BUOC dung -RunWhenLoggedOff (LogonType S4U): mac dinh task chi chay khi
  user dang dang nhap.

  Neu truoc do da dang ky task "tick" (register_invoice_tasks.ps1), go no di cho khoi chay trung:
    powershell -ExecutionPolicy Bypass -File scripts\register_invoice_tasks.ps1 -Unregister -TaskName "<ten>"

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\register_queue_tasks.ps1 -RunWhenLoggedOff
  powershell -ExecutionPolicy Bypass -File scripts\register_queue_tasks.ps1 -RunWhenLoggedOff -ListTimes 06:00,14:00,22:00 -MailEveryMinutes 60
  powershell -ExecutionPolicy Bypass -File scripts\register_queue_tasks.ps1 -RunWhenLoggedOff -NoWeInvoice
  powershell -ExecutionPolicy Bypass -File scripts\register_queue_tasks.ps1 -Unregister
#>
param(
    [string[]]$ListTimes = @("06:00", "14:00", "22:00"),
    [int]$MailEveryMinutes = 30,
    [string]$Prefix = "Invoice",
    [string]$Python = "",
    [switch]$RunWhenLoggedOff,
    [switch]$NoStart,
    [switch]$NoWeInvoice,
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"

$names = @{
    Sync = "$Prefix Sync Lists"; Mail = "$Prefix Mail"
    Xml  = "$Prefix Worker XML"; Pdf  = "$Prefix Worker PDF"
    Api  = "$Prefix WeInvoice API"; Winv = "$Prefix WeInvoice Worker"
}

if ($Unregister) {
    foreach ($n in $names.Values) {
        if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) {
            Stop-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $n -Confirm:$false
            Write-Output "Da xoa task '$n'"
        }
    }
    return
}

# `powershell -File` dua ca danh sach vao 1 chuoi -> tu tach (xem register_invoice_tasks.ps1)
$ListTimes = $ListTimes | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } |
             Where-Object { $_ }

$root = Split-Path -Parent $PSScriptRoot
$job = Join-Path $root "web\invoice_job.py"
if (-not (Test-Path $job)) { throw "Khong thay $job" }
if (-not (Test-Path (Join-Path $root ".env"))) {
    Write-Warning "Chua co file .env o $root (copy tu .env.example) - job se bao thieu DATABASE_URL"
}

if (-not $Python) {
    $py = (Get-Command python -ErrorAction Stop).Source
    if ($py -like "*\WindowsApps\*") {
        # Ban "App Execution Alias" cua Microsoft Store: trong phien chay ngam no dung im mai
        throw "python tro toi $py (alias cua Microsoft Store). Truyen -Python <duong dan pythonw.exe that>"
    }
    $pyw = Join-Path (Split-Path -Parent $py) "pythonw.exe"
    $Python = if (Test-Path $pyw) { $pyw } else { $py }
}

function New-JobAction([string]$cmd) {
    New-ScheduledTaskAction -Execute $Python -Argument "`"$job`" $cmd" -WorkingDirectory $root
}

function New-ScriptAction([string]$script) {
    $path = Join-Path $root "web\$script"
    if (-not (Test-Path $path)) { throw "Khong thay $path" }
    New-ScheduledTaskAction -Execute $Python -Argument "-X utf8 `"$path`"" -WorkingDirectory $root
}

$principal = $null
if ($RunWhenLoggedOff) {
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType S4U -RunLevel Limited
}

function Register-JobTask($name, $action, $triggers, $settings, $desc) {
    $p = @{ TaskName = $name; Action = $action; Trigger = $triggers; Settings = $settings
            Description = $desc; Force = $true }
    if ($principal) { $p.Principal = $principal }
    Register-ScheduledTask @p | Out-Null
}

# 1. Lay danh sach: chay ngan, khong chong chinh no (khoa SYNC trong PostgreSQL)
$syncSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-JobTask $names.Sync (New-JobAction "sync-lists") `
    ($ListTimes | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }) $syncSettings `
    "Lay danh sach HD tu GDT -> hop cho XML"

# 2. Mail: lap lai vo thoi han
$mailTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $MailEveryMinutes)
$mailSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-JobTask $names.Mail (New-JobAction "mail") $mailTrigger $mailSettings `
    "Doc mail hoa don (chi doc) -> khop XML/PDF vao HD"

# 3-4. Worker: chay mai, chet thi bat lai sau 1 phut
$workerTrigger = if ($RunWhenLoggedOff) { New-ScheduledTaskTrigger -AtStartup }
                 else { New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME" }
$workerSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-JobTask $names.Xml (New-JobAction "worker --type xml") $workerTrigger $workerSettings `
    "Rut hop cho XML lien tuc (GDT), xoay vong qua cac cong ty"
Register-JobTask $names.Pdf (New-JobAction "worker --type pdf") $workerTrigger $workerSettings `
    "Rut hop cho PDF lien tuc (site NCC), xoay vong qua cac cong ty"

# 5-6. WeInvoice: API noi bo (dang nhap thu GDT) + worker yeu cau cua backend, cung kieu chay mai
if (-not $NoWeInvoice) {
    Register-JobTask $names.Api (New-ScriptAction "internal_api.py") $workerTrigger $workerSettings `
        "API noi bo cho WeInvoice backend: dang nhap thu Cuc Thue khi them MST (log: output\logs\internal_api_*.log)"
    Register-JobTask $names.Winv (New-ScriptAction "weinvoice_worker.py") $workerTrigger $workerSettings `
        "Xu ly yeu cau WeInvoice: dong bo, lay XML/PDF, nhap file tai len, bo du lieu test"
}

Write-Output "Da dang ky:"
Write-Output "  $($names.Sync): $($ListTimes -join ', ')"
Write-Output "  $($names.Mail): moi $MailEveryMinutes phut"
Write-Output "  $($names.Xml), $($names.Pdf): chay mai"
if (-not $NoWeInvoice) { Write-Output "  $($names.Api), $($names.Winv): chay mai" }
Write-Output "Python: $Python"
if ($RunWhenLoggedOff) {
    Write-Output "Che do: chay ca khi da dang xuat (S4U), worker khoi dong cung may"
} else {
    Write-Output "Che do: CHI chay khi '$env:USERNAME' dang dang nhap. Tren may chu dung -RunWhenLoggedOff"
}

if (-not $NoStart) {
    Start-ScheduledTask -TaskName $names.Xml
    Start-ScheduledTask -TaskName $names.Pdf
    if (-not $NoWeInvoice) {
        Start-ScheduledTask -TaskName $names.Api
        Start-ScheduledTask -TaskName $names.Winv
    }
    Write-Output "Da bat cac task chay mai. Lay danh sach ngay: Start-ScheduledTask -TaskName '$($names.Sync)'"
}
