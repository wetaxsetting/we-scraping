<#
.SYNOPSIS
  Dang ky Task Scheduler cho 2 script tai PDF bang trinh duyet (can phien desktop).

.DESCRIPTION
  Tao 2 task, ten bat dau bang -Prefix (mac dinh "Invoice"):
    <Prefix> Batch FPT        scripts\fpt_batch.py        moi -EveryMinutes phut, --limit -FptLimit
    <Prefix> Batch M-Invoice  scripts\minvoice_batch.py  moi -EveryMinutes phut, --limit -MinvoiceLimit

  Hai cong nay bat reCAPTCHA v3 -> script mo Chromium CO GIAO DIEN (profile fpt_profile /
  minvoice_profile). Vi vay task dang ky kieu Interactive: CHI chay khi user dang dang nhap.
  May chu khong co phien desktop thi DUNG dang ky (hoac chi chay tay trong phien RDP).

  Khong dung -RunWhenLoggedOff (S4U) nhu register_queue_tasks.ps1: phien S4U khong co desktop,
  Chromium khong mo duoc, va reCAPTCHA v3 se cham diem thap.

  Script chi tai file vao <INVOICE_OUTPUT_DIR>\manual\{fpt,minvoice}; viec nhap DB do worker PDF
  (worker tu nhat file moi trong <= 60 s; fpt_batch/minvoice_batch con tu requeue dung HD vua lay).

  Log: output\logs\fpt_batch.log, output\logs\minvoice_batch.log (ghi noi tiep qua cac lan chay).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\register_batch_tasks.ps1
  powershell -ExecutionPolicy Bypass -File scripts\register_batch_tasks.ps1 -EveryMinutes 60 -MinvoiceLimit 100
  powershell -ExecutionPolicy Bypass -File scripts\register_batch_tasks.ps1 -Only FPT
  powershell -ExecutionPolicy Bypass -File scripts\register_batch_tasks.ps1 -StartNow
  powershell -ExecutionPolicy Bypass -File scripts\register_batch_tasks.ps1 -Unregister
#>
param(
    [int]$EveryMinutes = 30,
    [int]$FptLimit = 20,
    [int]$MinvoiceLimit = 50,
    [string]$Prefix = "Invoice",
    [string]$Python = "",
    [ValidateSet("Both", "FPT", "Minvoice")]
    [string]$Only = "Both",
    [switch]$StartNow,
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"

$names = @{
    Fpt = "$Prefix Batch FPT"; Minvoice = "$Prefix Batch M-Invoice"
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

if ($EveryMinutes -lt 10) { throw "-EveryMinutes phai >= 10 (moi lan mo trinh duyet)" }
if ($FptLimit -lt 1 -or $MinvoiceLimit -lt 1) { throw "-FptLimit / -MinvoiceLimit phai >= 1" }

$root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $root ".env"))) {
    Write-Warning "Chua co file .env o $root (job se bao thieu DATABASE_URL)"
}

$scripts = @{
    Fpt = Join-Path $root "scripts\fpt_batch.py"
    Minvoice = Join-Path $root "scripts\minvoice_batch.py"
}
foreach ($k in $scripts.Keys) {
    if (-not (Test-Path $scripts[$k])) { throw "Khong thay $($scripts[$k])" }
}

if (-not $Python) {
    $py = (Get-Command python -ErrorAction Stop).Source
    if ($py -like "*\WindowsApps\*") {
        # Alias cua Microsoft Store: phien chay ngam dung im mai
        throw "python tro toi $py (alias cua Microsoft Store). Truyen -Python <duong dan pythonw.exe that>"
    }
    $pyw = Join-Path (Split-Path -Parent $py) "pythonw.exe"
    $Python = if (Test-Path $pyw) { $pyw } else { $py }
}

$logDir = Join-Path $root "output\logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }

function New-BatchTask([string]$name, [string]$script, [string]$log, [int]$limit,
                       [int]$startAfterMinutes, [string]$desc) {
    $action = New-ScheduledTaskAction -Execute $Python `
        -Argument "-X utf8 `"$script`" --limit $limit --log `"$log`"" -WorkingDirectory $root
    # 2 task lech nhau 2 phut de khong mo 2 Chromium cung luc
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes($startAfterMinutes) `
        -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes)
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 90) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    # KHONG truyen Principal: mac dinh la "chi chay khi user dang dang nhap" (Interactive)
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
        -Description $desc -Force | Out-Null
    Write-Output "  $name : moi $EveryMinutes phut, limit $limit"
}

Write-Output "Da dang ky (chi chay khi '$env:USERNAME' dang dang nhap):"
if ($Only -ne "Minvoice") {
    New-BatchTask $names.Fpt $scripts.Fpt (Join-Path $logDir "fpt_batch.log") $FptLimit 2 `
        "Tai JSON /api/search cua FPT cho link mail (?mst=&sec=) -> hop cho adapter fpt"
}
if ($Only -ne "FPT") {
    New-BatchTask $names.Minvoice $scripts.Minvoice (Join-Path $logDir "minvoice_batch.log") `
        $MinvoiceLimit 4 "Tai PDF M-Invoice qua cong tra cuu (reCAPTCHA v3) -> hop cho adapter minvoice"
}
Write-Output "Python: $Python"
Write-Output "Log: $logDir\{fpt,minvoice}_batch.log"
Write-Output "Kiem tra: (Get-ScheduledTask -TaskName '$Prefix Batch *').Actions | Format-List Execute,Arguments,WorkingDirectory"
Write-Output "Chi chay khi dang nhap (khong S4U). May khong co desktop: dung dang ky."

if ($StartNow) {
    if ($Only -ne "Minvoice") { Start-ScheduledTask -TaskName $names.Fpt }
    if ($Only -ne "FPT") { Start-ScheduledTask -TaskName $names.Minvoice }
    Write-Output "Da bat 1 lan ngay."
}
