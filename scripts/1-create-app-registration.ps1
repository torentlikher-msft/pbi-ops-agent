<#
  Step 1 — Create the bot/Graph app registration and grant Graph permissions.

  Creates a single-tenant app registration used for: Bot Framework auth,
  Microsoft Graph (auto-install the Teams app + read the chat id), and
  Azure AI Foundry data-plane access (via client credentials).

  Requires: Azure CLI, and rights to create app registrations + grant admin
  consent (Application Administrator / Privileged Role Administrator or Global
  Admin). Run:  az login  first.
#>
[CmdletBinding()]
param(
  [string] $DisplayName = "PBI Ops Bot"
)

$ErrorActionPreference = "Stop"

Write-Host "Creating app registration '$DisplayName'..."
$app = az ad app create --display-name $DisplayName --sign-in-audience AzureADMyOrg | ConvertFrom-Json
$appId = $app.appId
Write-Host "  appId: $appId"

# Service principal (required for the bot + role assignments)
az ad sp create --id $appId | Out-Null

# Client secret (1 year)
$secret = az ad app credential reset --id $appId --years 1 --display-name "func" | ConvertFrom-Json
$clientSecret = $secret.password
$tenantId = (az account show --query tenantId -o tsv)

# Resolve Microsoft Graph app-role ids dynamically (avoids hard-coded GUIDs)
$graphSpId = "00000003-0000-0000-c000-000000000000"
$graph = az ad sp show --id $graphSpId | ConvertFrom-Json
$needed = @(
  "TeamsAppInstallation.ReadWriteForUser.All",
  "AppCatalog.Read.All",
  "Chat.ReadBasic.All",
  "User.Read.All"
)
foreach ($perm in $needed) {
  $role = $graph.appRoles | Where-Object { $_.value -eq $perm -and $_.allowedMemberTypes -contains "Application" }
  if (-not $role) { throw "Could not find Graph app role '$perm'." }
  Write-Host "  adding Graph permission $perm ($($role.id))"
  az ad app permission add --id $appId --api $graphSpId --api-permissions "$($role.id)=Role" | Out-Null
}

Write-Host "Granting admin consent (requires elevated rights)..."
az ad app permission admin-consent --id $appId

Write-Host ""
Write-Host "==================== SAVE THESE VALUES ===================="
Write-Host "botAppId       = $appId"
Write-Host "botAppTenantId = $tenantId"
Write-Host "botAppPassword = $clientSecret"
Write-Host "=========================================================="
Write-Host "Pass them to scripts/2-deploy-infra.ps1"
