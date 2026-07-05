# Landscape agent for Windows 11. Runs from a scheduled task every 2 minutes:
# checks in with inventory, executes queued tasks, reports results.
# Config: C:\ProgramData\Landscape\agent.json

$ErrorActionPreference = "Stop"
$ConfPath = "C:\ProgramData\Landscape\agent.json"
$AgentVersion = "1.0"

if (-not (Test-Path $ConfPath)) { Write-Error "not enrolled"; exit 1 }
$Conf = Get-Content $ConfPath | ConvertFrom-Json
$Headers = @{ "X-Host-Id" = "$($Conf.host_id)"; "X-Agent-Key" = $Conf.agent_key }

function Invoke-Api($Path, $Body) {
    Invoke-RestMethod -Uri "$($Conf.server)$Path" -Method Post -Headers $Headers `
        -ContentType "application/json" -Body ($Body | ConvertTo-Json -Depth 8 -Compress)
}

function Get-Inventory {
    $os  = Get-CimInstance Win32_OperatingSystem
    $cs  = Get-CimInstance Win32_ComputerSystem
    $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
    $nic = Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway } | Select-Object -First 1

    $pkgs = @()
    foreach ($root in @("HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*",
                        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*")) {
        $pkgs += Get-ItemProperty $root -ErrorAction SilentlyContinue |
            Where-Object { $_.DisplayName } |
            ForEach-Object { @{ name = $_.DisplayName; version = "$($_.DisplayVersion)" } }
    }

    $updates = @()
    try {
        $searcher = (New-Object -ComObject Microsoft.Update.Session).CreateUpdateSearcher()
        $result = $searcher.Search("IsInstalled=0 and Type='Software'")
        $updates = @($result.Updates | ForEach-Object {
            @{ name = $_.Title; version = ""; security = ($_.MsrcSeverity -ne $null) } })
    } catch {}

    @{
        hostname = $env:COMPUTERNAME
        os_version = "$($os.Caption) $($os.Version)"
        kernel = $os.Version
        arch = $env:PROCESSOR_ARCHITECTURE
        ip = if ($nic) { $nic.IPv4Address.IPAddress } else { "" }
        mac = if ($nic) { (Get-NetAdapter -InterfaceIndex $nic.InterfaceIndex).MacAddress.Replace("-", ":").ToLower() } else { "" }
        agent_version = $AgentVersion
        reboot_required = Test-Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired"
        hardware = @{
            cpu = $cpu.Name; cores = $cpu.NumberOfLogicalProcessors
            memory_mb = [int]($cs.TotalPhysicalMemory / 1MB)
            vendor = $cs.Manufacturer; model = $cs.Model
            disks = @(Get-CimInstance Win32_DiskDrive | ForEach-Object {
                @{ name = $_.Model; size = "{0:N0} GB" -f ($_.Size / 1GB) } })
            nics = @(Get-NetAdapter -Physical | ForEach-Object {
                @{ name = $_.Name; mac = $_.MacAddress.Replace("-", ":").ToLower(); ip = "" } })
        }
        packages = $pkgs
        updates = $updates
    }
}

function Invoke-AgentTask($Task) {
    $type = $Task.type; $p = $Task.payload
    switch ($type) {
        "pkg_install" {
            $out = ""; $failed = $false
            foreach ($pkg in $p.packages) {
                $out += (winget install -e --id $pkg --silent `
                    --accept-package-agreements --accept-source-agreements `
                    --source winget 2>&1 | Out-String)
                if ($LASTEXITCODE -ne 0) { $failed = $true }
            }
            return @{ status = if ($failed) { "failed" } else { "done" }
                      exit_code = if ($failed) { 1 } else { 0 }; output = $out }
        }
        "pkg_remove" {
            $out = ""; $failed = $false
            foreach ($pkg in $p.packages) {
                $out += (winget uninstall -e --id $pkg --silent 2>&1 | Out-String)
                if ($LASTEXITCODE -ne 0) { $failed = $true }
            }
            return @{ status = if ($failed) { "failed" } else { "done" }
                      exit_code = if ($failed) { 1 } else { 0 }; output = $out }
        }
        "script" {
            try {
                $out = Invoke-Expression $p.script 2>&1 | Out-String
                return @{ status = "done"; exit_code = 0; output = $out }
            } catch {
                return @{ status = "failed"; exit_code = 1; output = "$_" }
            }
        }
        "compliance_scan" {
            $results = @()
            foreach ($rule in $p.rules) {
                $ok = $false; $detail = ""
                try {
                    if ($rule.type -eq "powershell") {
                        Invoke-Expression $rule.cmd | Out-Null
                        $ok = ($LASTEXITCODE -eq 0 -or $?)
                        if ($LASTEXITCODE -ne $null -and $LASTEXITCODE -ne 0) { $ok = $false }
                    } else { $detail = "unsupported rule type $($rule.type) on windows" }
                } catch { $detail = "$_"; $ok = $false }
                $results += @{ id = $rule.id; desc = $rule.desc; ok = $ok; detail = $detail }
            }
            return @{ status = "done"; exit_code = 0
                      output = ($results | ConvertTo-Json -Depth 5 -Compress -AsArray) }
        }
        "reboot" {
            shutdown /r /t 60 /c "Landscape-requested reboot"
            return @{ status = "done"; exit_code = 0; output = "reboot scheduled" }
        }
        default { return @{ status = "failed"; exit_code = 1; output = "unsupported on windows: $type" } }
    }
}

try {
    $resp = Invoke-Api "/agent/checkin" (Get-Inventory)
    foreach ($task in $resp.tasks) {
        $r = Invoke-AgentTask $task
        Invoke-Api "/agent/result" @{ task_id = $task.id; status = $r.status
                                      exit_code = $r.exit_code; output = $r.output } | Out-Null
    }
} catch {
    Write-Host "checkin failed: $_"
    exit 1
}
