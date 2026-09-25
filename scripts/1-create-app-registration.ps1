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

# Allow public client flows so the device-code sign-in yields an appidacr=0
# Fabric token (the Power BI MCP rejects confidential-client tokens).
az ad app update --id $appId --set isFallbackPublicClient=true | Out-Null

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

# Delegated Fabric / Power BI scopes (resource: Power BI Service). The bot injects
# the user's Fabric token into the Foundry MCP tools, so these must be consented.
$pbiSpId = "00000009-0000-0000-c000-000000000000"
$pbi = az ad sp show --id $pbiSpId | ConvertFrom-Json
$fabricScopes = @(
  "Item.Read.All",
  "Item.Execute.All",
  "Dataset.Read.All",
  "Dataset.ReadWrite.All",
  "Report.Read.All",
  "SemanticModel.Read.All",
  "SemanticModel.ReadWrite.All",
  "SemanticModel.Execute.All",
  "Workspace.Read.All",
  "Catalog.Read.All"
)
foreach ($scopeValue in $fabricScopes) {
  $scope = $pbi.oauth2PermissionScopes | Where-Object { $_.value -eq $scopeValue }
  if (-not $scope) { throw "Could not find Power BI Service delegated scope '$scopeValue'." }
  Write-Host "  adding Fabric delegated scope $scopeValue ($($scope.id))"
  az ad app permission add --id $appId --api $pbiSpId --api-permissions "$($scope.id)=Scope" | Out-Null
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
