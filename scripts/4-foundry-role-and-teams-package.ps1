<#
  Step 4 — Grant the app registration data-plane access to the Foundry project,
  and (optionally) build the Teams app package zip for catalog upload.

  The role "Azure AI Developer" allows creating threads/runs against agents.
  Provide the full resource id of the Foundry project (or its parent account).
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)] [string] $BotAppId,
  [string] $FoundryResourceId = "/subscriptions/2bc549bc-9017-4a47-a8e0-12787c4bf604/resourceGroups/rg-pbi_ops_agent/providers/Microsoft.CognitiveServices/accounts/data-architect-project-resource"
)

$ErrorActionPreference = "Stop"

$spObjectId = az ad sp show --id $BotAppId --query id -o tsv
foreach ($role in @("Azure AI Developer", "Cognitive Services User")) {
  Write-Host "Assigning '$role' on $FoundryResourceId ..."
  az role assignment create `
    --assignee-object-id $spObjectId `
    --assignee-principal-type ServicePrincipal `
    --role $role `
    --scope $FoundryResourceId
}

# The app code reads/writes conversation state in Table Storage AS the service
# principal, so it needs table data access on the Functions storage account.
$storageId = az storage account list -g rg-pbi_ops_agent --query "[?starts_with(name,'pbiops')].id | [0]" -o tsv
if ($storageId) {
  Write-Host "Assigning 'Storage Table Data Contributor' to the SP on $storageId ..."
  az role assignment create `
    --assignee-object-id $spObjectId `
    --assignee-principal-type ServicePrincipal `
    --role "Storage Table Data Contributor" `
    --scope $storageId
}

# Build the Teams app package (manifest + icons) for catalog upload.
$teamsDir = Join-Path $PSScriptRoot "..\teams"
$zipPath = Join-Path $teamsDir "pbi-ops-agent.zip"
$required = @("manifest.json", "color.png", "outline.png")
$missing = $required | Where-Object { -not (Test-Path (Join-Path $teamsDir $_)) }
if ($missing) {
  Write-Warning "Skipping zip — missing files in teams\: $($missing -join ', '). Add 192x192 color.png and 32x32 outline.png, then re-run."
}
else {
  if (Test-Path $zipPath) { Remove-Item $zipPath }
  Compress-Archive -Path (Join-Path $teamsDir "manifest.json"), (Join-Path $teamsDir "color.png"), (Join-Path $teamsDir "outline.png") -DestinationPath $zipPath
  Write-Host "Created $zipPath — upload it in Teams Admin Center > Manage apps, or side-load for testing."
}
