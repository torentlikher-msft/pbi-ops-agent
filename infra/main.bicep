@description('Base name used to derive resource names (lowercase letters/numbers).')
param baseName string = 'pbiops'

@description('Location for all resources.')
param location string = resourceGroup().location

@description('Bot + Graph app registration (client) id.')
param botAppId string

@description('Tenant id of the bot app registration.')
param botAppTenantId string

@secure()
@description('Client secret for the bot app registration.')
param botAppPassword string

@description('Azure AI Foundry project endpoint, e.g. https://<res>.services.ai.azure.com/api/projects/<project>.')
param foundryProjectEndpoint string

@description('Hosted Foundry agent name (agent_reference), e.g. data-architect-agent.')
param foundryAgentName string

@description('Entra token audience for the Foundry data plane.')
param foundryTokenScope string = 'https://ai.azure.com/.default'

@description('externalId of the Teams app in the org catalog (== manifest id GUID).')
param teamsAppExternalId string

@description('Regional Teams service URL for cold-start proactive messages.')
param serviceUrlDefault string = 'https://smba.trafficmanager.net/amer/'

@secure()
@description('Shared API key required (x-api-key header) on the /api/notify trigger.')
param notifyApiKey string

@description('JSON array describing Monitoring Eventhouse query endpoints, databases, recipients, and optional thresholds.')
param eventhouseMonitorsJson string = ''

@description('NCRONTAB schedule for polling Monitoring Eventhouses. Default is once per minute.')
param queryAlertPollSchedule string = '0 * * * * *'

@description('Cooldown in seconds between slow-query notifications to the same recipient.')
param queryAlertCooldownSeconds int = 900

@description('Initial Eventhouse lookback window in minutes when no watermark exists.')
param queryAlertLookbackMinutes int = 5

@description('Maximum number of slow-query rows read from one Eventhouse per poll.')
param queryAlertMaxRows int = 100

var suffix = uniqueString(resourceGroup().id)
var storageName = toLower('${baseName}stg${suffix}')
var planName = '${baseName}-plan'
var functionAppName = '${baseName}-func-${suffix}'
var botName = '${baseName}-bot'
var appInsightsName = '${baseName}-ai'
var logAnalyticsName = '${baseName}-law'

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsName
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: 'Disabled'
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' = {
  name: '${baseName}-vnet'
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: [
        '10.20.0.0/16'
      ]
    }
    subnets: [
      {
        name: 'snet-func'
        properties: {
          addressPrefix: '10.20.0.0/27'
          delegations: [
            {
              name: 'flexdelegation'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
        }
      }
      {
        name: 'snet-pe'
        properties: {
          addressPrefix: '10.20.0.32/27'
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
    ]
  }
}

resource funcSubnet 'Microsoft.Network/virtualNetworks/subnets@2023-11-01' existing = {
  parent: vnet
  name: 'snet-func'
}

resource peSubnet 'Microsoft.Network/virtualNetworks/subnets@2023-11-01' existing = {
  parent: vnet
  name: 'snet-pe'
}

var storageDnsServices = [
  'blob'
  'queue'
  'table'
]

resource storageDnsZones 'Microsoft.Network/privateDnsZones@2020-06-01' = [
  for svc in storageDnsServices: {
    name: 'privatelink.${svc}.${environment().suffixes.storage}'
    location: 'global'
  }
]

resource storageDnsLinks 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = [
  for (svc, i) in storageDnsServices: {
    parent: storageDnsZones[i]
    name: 'link-to-vnet'
    location: 'global'
    properties: {
      registrationEnabled: false
      virtualNetwork: {
        id: vnet.id
      }
    }
  }
]

resource storagePrivateEndpoints 'Microsoft.Network/privateEndpoints@2023-11-01' = [
  for svc in storageDnsServices: {
    name: '${baseName}-pe-${svc}'
    location: location
    properties: {
      subnet: {
        id: peSubnet.id
      }
      privateLinkServiceConnections: [
        {
          name: svc
          properties: {
            privateLinkServiceId: storage.id
            groupIds: [
              svc
            ]
          }
        }
      ]
    }
  }
]

resource storagePeDnsGroups 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2023-11-01' = [
  for (svc, i) in storageDnsServices: {
    parent: storagePrivateEndpoints[i]
    name: 'default'
    properties: {
      privateDnsZoneConfigs: [
        {
          name: svc
          properties: {
            privateDnsZoneId: storageDnsZones[i].id
          }
        }
      ]
    }
  }
]

resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: planName
  location: location
  kind: 'functionapp'
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  properties: {
    reserved: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource deploymentContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'deploymentpackage'
}

resource functionApp 'Microsoft.Web/sites@2023-12-01' = {
  name: functionAppName
  location: location
  kind: 'functionapp,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    virtualNetworkSubnetId: funcSubnet.id
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}${deploymentContainer.name}'
          authentication: {
            type: 'SystemAssignedIdentity'
          }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: 40
        instanceMemoryMB: 2048
      }
      runtime: {
        name: 'python'
        version: '3.11'
      }
    }
    siteConfig: {
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      cors: {
        allowedOrigins: [
          'https://portal.azure.com'
        ]
      }
      appSettings: [
        {
          name: 'AzureWebJobsStorage__accountName'
          value: storage.name
        }
        {
          name: 'STORAGE_ACCOUNT_NAME'
          value: storage.name
        }
        {
          name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
          value: appInsights.properties.ConnectionString
        }
        {
          name: 'MicrosoftAppType'
          value: 'SingleTenant'
        }
        {
          name: 'MicrosoftAppId'
          value: botAppId
        }
        {
          name: 'MicrosoftAppPassword'
          value: botAppPassword
        }
        {
          name: 'MicrosoftAppTenantId'
          value: botAppTenantId
        }
        {
          name: 'FOUNDRY_PROJECT_ENDPOINT'
          value: foundryProjectEndpoint
        }
        {
          name: 'FOUNDRY_AGENT_NAME'
          value: foundryAgentName
        }
        {
          name: 'FOUNDRY_TOKEN_SCOPE'
          value: foundryTokenScope
        }
        {
          name: 'TEAMS_APP_EXTERNAL_ID'
          value: teamsAppExternalId
        }
        {
          name: 'TEAMS_SERVICE_URL_DEFAULT'
          value: serviceUrlDefault
        }
        {
          name: 'NOTIFY_API_KEY'
          value: notifyApiKey
        }
        {
          name: 'EVENTHOUSE_MONITORS_JSON'
          value: eventhouseMonitorsJson
        }
        {
          name: 'QUERY_ALERT_POLL_SCHEDULE'
          value: queryAlertPollSchedule
        }
        {
          name: 'QUERY_ALERT_COOLDOWN_SECONDS'
          value: string(queryAlertCooldownSeconds)
        }
        {
          name: 'QUERY_ALERT_LOOKBACK_MINUTES'
          value: string(queryAlertLookbackMinutes)
        }
        {
          name: 'QUERY_ALERT_MAX_ROWS'
          value: string(queryAlertMaxRows)
        }
        {
          name: 'OAUTH_CONNECTION_NAME'
          value: 'fabric'
        }
      ]
    }
  }
}

var storageBlobDataOwnerId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b')
var storageQueueDataContributorId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '974c5e8b-45b9-4653-ba55-5f855dd0fb88')
var storageTableDataContributorId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3')

resource blobOwnerAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, functionApp.id, storageBlobDataOwnerId)
  properties: {
    roleDefinitionId: storageBlobDataOwnerId
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource queueContributorAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, functionApp.id, storageQueueDataContributorId)
  properties: {
    roleDefinitionId: storageQueueDataContributorId
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource tableContributorAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, functionApp.id, storageTableDataContributorId)
  properties: {
    roleDefinitionId: storageTableDataContributorId
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource bot 'Microsoft.BotService/botServices@2022-09-15' = {
  name: botName
  location: 'global'
  kind: 'azurebot'
  sku: {
    name: 'F0'
  }
  properties: {
    displayName: botName
    endpoint: 'https://${functionApp.properties.defaultHostName}/api/messages'
    msaAppId: botAppId
    msaAppType: 'SingleTenant'
    msaAppTenantId: botAppTenantId
  }
}

resource teamsChannel 'Microsoft.BotService/botServices/channels@2022-09-15' = {
  parent: bot
  name: 'MsTeamsChannel'
  location: 'global'
  properties: {
    channelName: 'MsTeamsChannel'
    properties: {
      isEnabled: true
    }
  }
}

output functionAppName string = functionApp.name
output functionAppHostName string = functionApp.properties.defaultHostName
output messagesEndpoint string = 'https://${functionApp.properties.defaultHostName}/api/messages'
output notifyEndpoint string = 'https://${functionApp.properties.defaultHostName}/api/notify'
output functionSystemIdentityPrincipalId string = functionApp.identity.principalId
