<#
  Step 2 — Deploy the Azure infrastructure (Function App, Bot, Storage, App Insights)
  into the resource group with Bicep.

  Run:  az login  first, and select the correct subscription:
        az account set --subscription "<sub id or name>"
#>
[CmdletBinding()]
param(
  [string] $BaseName = "pbiops",
  
  [Parameter(Mandatory = $true)] [string] $ResourceGroup,
  [Parameter(Mandatory = $true)] [string] $BotAppId,
  [Parameter(Mandatory = $true)] [string] $BotAppTenantId,
  [Parameter(Mandatory = $true)] [string] $BotAppPassword,

  [string] $FoundryProjectEndpoint = "https://pbi-ops-project-resource.services.ai.azure.com/api/projects/pbi-ops-project",
  [string] $FoundryAgentName = "pbi-ops-agent",

  [string] $TeamsAppExternalId = "b6f1e2a4-7c3d-4e58-9a1b-2c3d4e5f6a7b",
  [string] $ServiceUrlDefault = "https://smba.trafficmanager.net/amer/",
  [Parameter(Mandatory = $true)] [string] $NotifyApiKey,

  [string] $EventhouseMonitorsJson = "",
  [int] $QueryAlertCooldownSeconds = 900,
  [int] $QueryAlertLookbackMinutes = 5,
  [int] $QueryAlertMaxRows = 100
)

$ErrorActionPreference = "Stop"
$templatePath = Join-Path $PSScriptRoot "..\infra\main.bicep"

Write-Host "Deploying infra to $ResourceGroup ..."
az deployment group create `
  --resource-group $ResourceGroup `
  --template-file $templatePath `
  --parameters `
    baseName=$BaseName `
    botAppId=$BotAppId `
    botAppTenantId=$BotAppTenantId `
    botAppPassword=$BotAppPassword `
    foundryProjectEndpoint=$FoundryProjectEndpoint `
    foundryAgentName=$FoundryAgentName `
    teamsAppExternalId=$TeamsAppExternalId `
    serviceUrlDefault=$ServiceUrlDefault `
    notifyApiKey=$NotifyApiKey `
    eventhouseMonitorsJson=$EventhouseMonitorsJson `
    queryAlertCooldownSeconds=$QueryAlertCooldownSeconds `
    queryAlertLookbackMinutes=$QueryAlertLookbackMinutes `
    queryAlertMaxRows=$QueryAlertMaxRows `
  --query "properties.outputs" -o jsonc
