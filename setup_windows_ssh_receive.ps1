param(
    [string]$ReceivePath = "E:\chamber_data_share"
)

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "Run this script from an elevated PowerShell window."
    exit 1
}

New-Item -ItemType Directory -Force -Path $ReceivePath | Out-Null

$capability = Get-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
if ($capability.State -ne "Installed") {
    Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 | Out-Null
}

Start-Service sshd
Set-Service -Name sshd -StartupType Automatic

if (-not (Get-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule `
        -Name "OpenSSH-Server-In-TCP" `
        -DisplayName "OpenSSH Server (sshd)" `
        -Enabled True `
        -Direction Inbound `
        -Protocol TCP `
        -Action Allow `
        -LocalPort 22 | Out-Null
}

Get-NetConnectionProfile |
    Where-Object { $_.NetworkCategory -eq "Public" } |
    Set-NetConnectionProfile -NetworkCategory Private

Write-Host "OpenSSH Server is ready."
Write-Host "Use this on Raspberry Pi:"
Write-Host "  python3 migrate_to.py --method ssh --pc-dir /E:/chamber_data_share"
Write-Host ""
Write-Host "If that Windows path does not work with your OpenSSH version, use:"
Write-Host "  python3 migrate_to.py --method ssh --pc-dir E:/chamber_data_share"
