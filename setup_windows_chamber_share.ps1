param(
    [string]$SharePath = "E:\chamber_data_share",
    [string]$ShareName = "chamber_data_share",
    [string]$User = "$env:COMPUTERNAME\$env:USERNAME"
)

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "Run this script from an elevated PowerShell window."
    exit 1
}

New-Item -ItemType Directory -Force -Path $SharePath | Out-Null

$acl = Get-Acl -Path $SharePath
$rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
    $User,
    "Modify",
    "ContainerInherit,ObjectInherit",
    "None",
    "Allow"
)
$acl.SetAccessRule($rule)
Set-Acl -Path $SharePath -AclObject $acl

$existingShare = Get-SmbShare -Name $ShareName -ErrorAction SilentlyContinue
if ($existingShare) {
    Set-SmbShare -Name $ShareName -Description "Temperature chamber data share for Raspberry Pi migration" -Force | Out-Null
    Grant-SmbShareAccess -Name $ShareName -AccountName $User -AccessRight Change -Force | Out-Null
} else {
    New-SmbShare `
        -Name $ShareName `
        -Path $SharePath `
        -ChangeAccess $User `
        -Description "Temperature chamber data share for Raspberry Pi migration" | Out-Null
}

Get-NetFirewallRule |
    Where-Object {
        $_.DisplayGroup -eq "File and Printer Sharing" -or
        $_.DisplayGroup -like "*파일*프린터*" -or
        $_.Group -eq "@FirewallAPI.dll,-28502"
    } |
    Enable-NetFirewallRule

Write-Host "SMB share is ready:"
Write-Host "  \\$env:COMPUTERNAME\$ShareName"
Write-Host "  //$((Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.PrefixOrigin -ne 'WellKnown' } | Select-Object -First 1 -ExpandProperty IPAddress))/$ShareName"
