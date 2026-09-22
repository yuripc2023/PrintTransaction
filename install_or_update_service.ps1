[CmdletBinding()]
param(
    [string]$PythonExe = "python",
    [string]$ServiceName = ""
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host "==> $Message"
}

function Test-IsAdministrator {
    $currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($currentIdentity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Invoke-ServiceCommand {
    param([string[]]$Arguments)

    & $PythonExe .\print_server_service.py @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Fallo el comando: $PythonExe .\print_server_service.py $($Arguments -join ' ')"
    }
}

if (-not (Test-IsAdministrator)) {
    throw "Este script debe ejecutarse como Administrador."
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

Write-Step "Validando Python"
& $PythonExe --version | Out-Null

Write-Step "Validando wrapper del servicio"
$pythonServiceName = (& $PythonExe -c "import print_server_service; print(print_server_service.PrintServerWindowsService._svc_name_)").Trim()
if (-not $ServiceName) {
    $ServiceName = $pythonServiceName
}
if ($ServiceName -ne $pythonServiceName) {
    throw "ServiceName='$ServiceName' no coincide con el nombre definido en print_server_service.py: '$pythonServiceName'."
}

$existingService = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue

if ($null -ne $existingService) {
    Write-Step "Servicio existente detectado: $ServiceName"

    if ($existingService.Status -ne "Stopped") {
        Write-Step "Deteniendo servicio"
        Invoke-ServiceCommand @("--wait", "30", "stop")
    }

    Write-Step "Eliminando instalacion anterior"
    Invoke-ServiceCommand @("remove")
    Start-Sleep -Seconds 2
}

Write-Step "Instalando servicio"
Invoke-ServiceCommand @("--startup", "auto", "install")

Write-Step "Iniciando servicio"
Invoke-ServiceCommand @("--wait", "30", "start")

Write-Step "Estado final del servicio"
Get-Service -Name $ServiceName | Format-Table -AutoSize

Write-Step "Proceso completado"
