<#
.SYNOPSIS
  Dang ky 1 task Scheduler chay tuan tu 2 script tai PDF bang trinh duyet (can phien desktop).

.DESCRIPTION
  Tao 1 task "<Prefix> Batch Browser" goi scripts\batch_all.py:
      fpt_batch.py --limit <FptLimit>       (truoc)
      minvoice_batch.py --limit <MinvoiceLimit>  (sau)

  Hai cong nay bat reCAPTCHA v3 -> script mo Chromium CO GIAO DIEN (profile fpt_profile /
  minvoice_profile). Vi vay task dang ky kieu Interactive: CHI chay khi user dang dang nhap.
  May chu khong co phien desktop thi DUNG dang ky (hoac chi chay tay trong phien RDP).

  Khong dung -RunWhenLoggedOff (S4U) nhu register_queue_tasks.ps1: phien S4U khong co desktop,
  Chromium khong mo duoc.

  Script chi tai file vao <INVOICE_OUTPUT_DIR>\manual\{fpt,minvoice}; worker PDF tu nhat file moi
  trong <= 60 s (khong can --import).

  Log: output\logs\fpt_batch.log, output\logs\minvoice_batch.log (noi tiep qua cac lan chay).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\register_batch_tasks.ps1
  powershell -ExecutionPolicy Bypass -File scripts\register_batch_tasks.ps1 -EveryMinutes 60 -MinvoiceLimit 100
  powershell -ExecutionPolicy Bypass -File scripts\register_batch_tasks.ps1 -Only Minvoice
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

$name = "$Prefix Batch Browser"
# Ten cu (ban 2 task rieng) — go luon khi -Unregister / dang ky lai
$legacy = @("$Prefix Batch FPT", "$Prefix Batch M-Invoice")

if ($Unregister) {
    foreach ($n in @($name) + $legacy) {
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

$runner = Join-Path $root "scripts\batch_all.py"
if (-not (Test-Path $runner)) { throw "Khong thay $runner" }

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

# Go task cu (neu co) de khong chay trung
foreach ($n in $legacy) {
    if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $n -Confirm:$false
        Write-Output "Da go task cu '$n'"
    }
}

$onlyArg = if ($Only -eq "Both") { "" } else { " --only $($Only.ToLower())" }
$action = New-ScheduledTaskAction -Execute $Python `
    -Argument "-X utf8 `"$runner`" --fpt-limit $FptLimit --minvoice-limit $MinvoiceLimit --log-dir `"$logDir`"$onlyArg" `
    -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 90) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

# KHONG truyen Principal: mac dinh "chi chay khi user dang dang nhap" (Interactive)
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
    -Description "Tai JSON/PDF FPT + M-Invoice bang Chromium (reCAPTCHA v3), tuan tu; worker PDF tu nhap" `
    -Force | Out-Null

Write-Output "Da dang ky (chi chay khi '$env:USERNAME' dang dang nhap):"
Write-Output "  $name : moi $EveryMinutes phut (FPT $FptLimit, M-Invoice $MinvoiceLimit, only=$Only)"
Write-Output "Python: $Python"
Write-Output "Log: $logDir\{fpt,minvoice}_batch.log"
Write-Output "Kiem tra: (Get-ScheduledTask -TaskName '$name').Actions | Format-List Execute,Arguments,WorkingDirectory"
Write-Output "May khong co desktop: dung dang ky (phien S4U khong mo duoc Chromium)."

if ($StartNow) {
    Start-ScheduledTask -TaskName $name
    Write-Output "Da bat 1 lan ngay."
}
