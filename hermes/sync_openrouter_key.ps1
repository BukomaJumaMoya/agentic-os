<#
.SYNOPSIS
  Put one OpenRouter key into the four places that need it, without it ever
  appearing on a command line or in a transcript.

.DESCRIPTION
  Four processes call OpenRouter: Hermes itself, and the three agents. Each
  reads its own file by design, so a new key has to land in four places. Doing
  that by hand is four chances to paste the wrong thing into the wrong file.

  The key is read from the OPENROUTER_API_KEY User environment variable, or
  prompted for as a secure string. It is never echoed, never passed as an
  argument, and never written to a log. The script validates it against
  OpenRouter before writing anything, so an invalid key fails here rather than
  silently at the first Telegram message.

.EXAMPLE
  # Set the variable once, then distribute it:
  [Environment]::SetEnvironmentVariable('OPENROUTER_API_KEY','sk-or-v1-...','User')
  .\hermes\sync_openrouter_key.ps1

.EXAMPLE
  # Or be prompted (input is masked):
  .\hermes\sync_openrouter_key.ps1 -Prompt
#>
[CmdletBinding()]
param(
    [switch]$Prompt,
    [string]$HermesHome = $(if ($env:HERMES_HOME) { $env:HERMES_HOME } else { "$env:LOCALAPPDATA\hermes" })
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot

# --- obtain the key ------------------------------------------------------
if ($Prompt) {
    $secure = Read-Host -Prompt "OpenRouter API key" -AsSecureString
    $key = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
} else {
    $key = [Environment]::GetEnvironmentVariable('OPENROUTER_API_KEY', 'User')
    if (-not $key) {
        Write-Host "OPENROUTER_API_KEY is not set for your user account."
        Write-Host "Either set it, or re-run this script with -Prompt."
        exit 1
    }
}

$key = $key.Trim()
if (-not $key) { Write-Host "No key supplied."; exit 1 }

# --- validate before writing anything ------------------------------------
Write-Host "Validating the key against OpenRouter..."
try {
    $info = Invoke-RestMethod -Uri "https://openrouter.ai/api/v1/key" `
        -Headers @{ Authorization = "Bearer $key" } -TimeoutSec 30
    Write-Host ("  OK  label={0}  free_tier={1}  usage={2}" -f `
        $info.data.label, $info.data.is_free_tier, $info.data.usage)
} catch {
    Write-Host "  REJECTED by OpenRouter. Nothing was written."
    Write-Host "  Create a new key at https://openrouter.ai/settings/keys"
    exit 1
}

# --- write it into each file that needs it -------------------------------
function Set-EnvKey {
    param([string]$Path, [string]$Name, [string]$Value)

    if (-not (Test-Path $Path)) {
        Write-Host "  SKIP  $Path (does not exist)"
        return
    }
    $lines = @(Get-Content -LiteralPath $Path)
    $found = $false
    $out = foreach ($line in $lines) {
        if ($line -match "^\s*$Name\s*=") { $found = $true; "$Name=$Value" }
        else { $line }
    }
    if (-not $found) { $out = @($out) + "$Name=$Value" }
    Set-Content -LiteralPath $Path -Value $out -Encoding utf8
    Write-Host "  SET   $Name in $Path"
}

$targets = @(
    "$repo\agents\research\.env",
    "$repo\agents\pm\.env",
    "$repo\agents\coding\.env",
    "$HermesHome\.env"
)
foreach ($t in $targets) { Set-EnvKey -Path $t -Name 'OPENROUTER_API_KEY' -Value $key }

Write-Host ""
Write-Host "Done. Restart the gateway so Hermes picks up its own copy:"
Write-Host "  hermes gateway stop; Start-ScheduledTask -TaskName Hermes_Gateway"
