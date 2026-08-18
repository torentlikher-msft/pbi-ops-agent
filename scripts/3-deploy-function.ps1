<#
  Step 3 — Publish the Python function code to the Function App.

  This environment disables storage shared-key access by policy, so:
    * the app uses managed identity / RBAC for storage (handled in Bicep), and
    * we CANNOT use remote (Oryx) build, which stages via storage keys.
  Instead we prefetch Linux (manylinux) wheels locally and deploy with --no-build.

  Requires Azure Functions Core Tools v4:
    https://learn.microsoft.com/azure/azure-functions/functions-run-local
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)] [string] $FunctionAppName
)

$ErrorActionPreference = "Stop"
$srcPath = Join-Path $PSScriptRoot "..\src"

Push-Location $srcPath
try {
  Write-Host "Prefetching Linux (py3.11) wheels into .python_packages..."
  if (Test-Path ".python_packages") { Remove-Item ".python_packages" -Recurse -Force }
  python -m pip install --target ".python_packages/lib/site-packages" `
    --only-binary=:all: --platform manylinux2014_x86_64 `
    --python-version 3.11 --implementation cp --abi cp311 `
    -r requirements.txt

  Write-Host "Publishing to $FunctionAppName (no remote build)..."
  func azure functionapp publish $FunctionAppName --python --no-build
}
finally {
  Pop-Location
}
