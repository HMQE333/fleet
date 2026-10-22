# Put an API key into .env without it ever appearing on the command line or in the shell history.
#   .\scripts\set_key.ps1                      # asks for ANTHROPIC_API_KEY
#   .\scripts\set_key.ps1 OPENROUTER_API_KEY   # any NAME=... line of .env
param([string]$Name = "ANTHROPIC_API_KEY")

$ErrorActionPreference = "Stop"
$envFile = Join-Path (Split-Path -Parent $PSScriptRoot) ".env"
if (-not (Test-Path $envFile)) { Copy-Item (Join-Path (Split-Path -Parent $PSScriptRoot) ".env.example") $envFile }

$secure = Read-Host "Paste the value for $Name (input hidden)" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try { $value = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr).Trim() }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
if (-not $value) { Write-Host "Nothing entered; .env unchanged."; exit 1 }

$lines = Get-Content $envFile -Encoding UTF8
$pattern = "^$([regex]::Escape($Name))="
if ($lines -match $pattern) {
  $lines = $lines | ForEach-Object { if ($_ -match $pattern) { "$Name=$value" } else { $_ } }
} else {
  $lines += "$Name=$value"
}
[IO.File]::WriteAllLines($envFile, $lines, (New-Object Text.UTF8Encoding $false))
$hint = if ($value.Length -ge 12) { "..." + $value.Substring($value.Length - 4) } else { "(short value)" }
Write-Host "Saved $Name $hint to .env ($($value.Length) characters). .env is git-ignored."
