# Landscape Windows agent installer. Run as Administrator:
#   .\install.ps1 -Server http://<server>:8443 -Token <ENROLL_TOKEN>
param(
    [Parameter(Mandatory)][string]$Server,
    [Parameter(Mandatory)][string]$Token,
    [int]$HttpPort = 8081
)
$ErrorActionPreference = "Stop"

$Dir = "C:\ProgramData\Landscape"
New-Item -ItemType Directory -Force -Path $Dir | Out-Null

$ServerHost = ([uri]$Server).Host
Invoke-WebRequest "http://${ServerHost}:${HttpPort}/agent-src/windows/agent.ps1" `
    -OutFile "$Dir\agent.ps1" -UseBasicParsing

# Enroll
$machineId = (Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Cryptography").MachineGuid
$os = Get-CimInstance Win32_OperatingSystem
$nic = Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway } | Select-Object -First 1
$body = @{
    token = $Token; machine_id = $machineId; hostname = $env:COMPUTERNAME
    os_family = "windows"; os_version = "$($os.Caption) $($os.Version)"
    kernel = $os.Version; arch = $env:PROCESSOR_ARCHITECTURE
    ip = if ($nic) { $nic.IPv4Address.IPAddress } else { "" }
    mac = if ($nic) { (Get-NetAdapter -InterfaceIndex $nic.InterfaceIndex).MacAddress.Replace("-", ":").ToLower() } else { "" }
    agent_version = "1.0"
} | ConvertTo-Json -Compress
$resp = Invoke-RestMethod -Uri "$Server/agent/enroll" -Method Post `
    -ContentType "application/json" -Body $body

@{ server = $Server; host_id = $resp.host_id; agent_key = $resp.agent_key } |
    ConvertTo-Json | Set-Content "$Dir\agent.json"

# Scheduled task: run every 2 minutes as SYSTEM.
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Dir\agent.ps1`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 2)
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName "LandscapeAgent" -Action $action -Trigger $trigger `
    -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName "LandscapeAgent"

Write-Host "Landscape agent installed — enrolled as host $($resp.host_id)."
