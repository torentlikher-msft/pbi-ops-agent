# Power BI Ops Agent — Proactive Operations

Adds an **outbound / proactive** channel to your existing Azure AI Foundry agent.
An HTTP `POST /api/notify` triggers the agent to message a specific Teams user
(like an operations bot), and the user can then ask follow-up questions in the
same thread.

This is a **separate bot + Function App** because the bot that Foundry
auto-provisions when you publish to Teams can only *reply* to inbound messages —
it cannot start a conversation from an external trigger or keep proactive thread
state.

## How it works

```
POST /api/notify ──▶ Function App ──▶ (Graph: auto-install app + get chat id if new user)
                                   └─▶ Bot Framework CloudAdapter ──▶ Teams user (proactive)
                                   └─▶ Foundry agent (Responses API) ──▶ your PBI agent

Teams user reply ──▶ POST /api/messages ──▶ same conversation (previous_response_id) ──▶ agent answer
```

State lives in Azure Table Storage:
- `convrefs` — ConversationReference per user (AAD object id)
- `respmap` — last Foundry `response_id` per Teams conversation (enables follow-ups via `previous_response_id`)

### Agent integration

The published agent (`data-architect-agent`) is a Foundry **prompt agent** exposed
through the **responses** protocol. It is invoked at
`{FOUNDRY_PROJECT_ENDPOINT}/openai/v1/responses` with an `agent_reference` body and
Entra auth (audience `https://ai.azure.com/.default`). Follow-ups pass
`previous_response_id`, so Foundry keeps the conversation state server-side.

## Resources created (in `rg-pbi_ops_agent`)

| Resource | Purpose |
|---|---|
| Function App (Linux, Python 3.11, Consumption) | hosts `/api/messages` + `/api/notify` |
| Azure Bot (`azurebot`, Teams channel) | bot registration + Teams routing |
| Storage account | Functions runtime + Table state |
| Application Insights + Log Analytics | telemetry |
| App registration (via script) | Bot auth + Graph + Foundry access |

## Deployed instance (rg-pbi_ops_agent)

Already provisioned and verified in `rg-pbi_ops_agent` (eastus2):

| Item | Value |
|---|---|
| Function App | `pbiops-func-zwxa2ieqj5e6q` |
| messages endpoint | `https://pbiops-func-zwxa2ieqj5e6q.azurewebsites.net/api/messages` |
| notify endpoint | `https://pbiops-func-zwxa2ieqj5e6q.azurewebsites.net/api/notify` |
| Azure Bot | `pbiops-bot` (Teams channel enabled) |
| Storage | `pbiopsstgzwxa2ieqj5e6q` (shared-key disabled, RBAC only) |
| App registration | `717de829-3e58-42c5-af5b-8d68a5d644cc` |
| Foundry agent | `data-architect-agent` (prompt agent, Responses API) |

Get the notify key: `az functionapp keys list -g rg-pbi_ops_agent -n pbiops-func-zwxa2ieqj5e6q --query "functionKeys.default" -o tsv`
The `x-api-key` (NOTIFY_API_KEY) is stored as an app setting on the Function App.

### Remaining step to enable proactive delivery

The pipeline is verified working end-to-end; the only remaining step is an
**org admin** action: package and publish the Teams app so Graph can auto-install
it for target users.

1. Add `color.png` (192×192) and `outline.png` (32×32) to `teams/`.
2. `teams/manifest.json` already has `botId` = the app id and `id` = the
   `TEAMS_APP_EXTERNAL_ID`. Zip manifest + icons (see `scripts/4-...ps1`).
3. Upload the zip in **Teams Admin Center → Manage apps** (or side-load for testing).

Until then, `POST /api/notify` returns `502` with *"Teams app ... not found in the
org app catalog"* — which confirms auth, storage, Graph, and the agent call all work.

## Environment constraints baked into this solution

- **Storage shared-key access is disabled by policy** → the Function App uses its
  **system-assigned managed identity** for `AzureWebJobsStorage`/secrets, and the
  app code uses the **bot service principal** for Table Storage. Do NOT set
  `AZURE_CLIENT_ID/AZURE_TENANT_ID/AZURE_CLIENT_SECRET` app settings — the host
  would then authenticate storage as the SP (which lacks blob access) and fail.
- **Storage public network access is disabled by policy** → the storage account is
  reached over **private endpoints** (blob/queue/table) from a VNet, and the
  Function App is **VNet-integrated** (subnet delegated to `Microsoft.App/environments`).
  Without this, Flex Consumption instances can't reach storage on cold start and the
  host fails to start (symptom: `503 The service is unavailable`, no telemetry).
  Foundry and App Insights remain public, so the agent call and logging work directly.
- **No remote (Oryx) build** — it stages via storage keys. Deploy prefetches Linux
  wheels locally and publishes with `--no-build` (see `scripts/3-...ps1`).
- Hosting is **Flex Consumption (FC1)** because the subscription has 0 dynamic VM
  quota for classic Consumption.

## Trigger contract

```http
POST https://<funcapp>.azurewebsites.net/api/notify?code=<function-key>
Content-Type: application/json
x-api-key: <NOTIFY_API_KEY>

{
  "userId": "<AAD object id of the target user>",
  "message": "Prompt/instruction the agent should act on",
  "sendRaw": false
}
```
- `sendRaw: false` (default) → runs the prompt through the Foundry agent and sends the answer.
- `sendRaw: true` → sends `message` verbatim (no agent call).
- Response `202` once the proactive message is dispatched.

## Automatic slow-query alerts

The Function App includes a one-minute timer trigger that can poll multiple
Fabric Monitoring Eventhouses. It reads new rows from `SemanticModelLogs` and,
for each recipient, sends a casual, conversational opener that offers help — e.g.
*"Greetings! I noticed a sub-optimal query originating from the **X** report and
**Y** semantic model. Would you like some help troubleshooting and optimizing
it?"* The slowest ("offending") query in the window — its full `EventText` (DAX),
model and resolved report name — is persisted as **pending analysis** context in
the `pendinganalysis` Azure Table. The user's reply is classified by the model
itself (a lightweight, tool-free intent call — `accept` / `decline` / `other`),
so natural phrasings are understood rather than matched against a fixed keyword
list; a deterministic keyword heuristic is used only as a fallback if the model
call fails. On `accept`, the bot runs the same analysis workflow as a normal
question, but grounded **in that query**: `EventText` is the DAX auto-generated by
the Power BI engine for the visual, so the agent is told to treat it as evidence
of the user's intended measures and groupings (not a hand-written query) and to
focus on the semantic model rather than scanning the whole model. A `decline`
clears the offer; any other reply is treated as a new question. A durable
per-monitor/per-recipient cooldown and watermarks are stored in the `querymonitor`
Azure Table, so they survive restarts and scale-out.

Set `EVENTHOUSE_MONITORS_JSON` to a JSON array. The preferred configuration uses
the workspace and KQL database item ids; the Function resolves the query URI.
With `notifyModelOwner`, it reads each model's `configuredBy` property from the
Power BI REST API and resolves that UPN to an Entra object id through Graph:

```json
[
  {
    "name": "Production monitoring",
    "workspaceId": "1fea49ee-a4a1-48ad-b431-431a9cb613df",
    "kqlDatabaseId": "0f969a09-faaf-40a2-9269-890686804944",
    "notifyModelOwner": true,
    "durationMs": 30000
  }
]
```

Relevant settings:

| Setting | Default | Purpose |
|---|---:|---|
| `QUERY_ALERT_POLL_SCHEDULE` | `0 * * * * *` | Azure Functions NCRONTAB; once per minute |
| `QUERY_ALERT_COOLDOWN_SECONDS` | `900` | Minimum 15 minutes between alerts to a recipient |
| `QUERY_ALERT_LOOKBACK_MINUTES` | `5` | Initial lookback before a watermark exists |
| `QUERY_ALERT_MAX_ROWS` | `100` | Maximum qualifying rows per Eventhouse poll |

### Triggering a poll on demand

Besides the timer, the Function App exposes an HTTP endpoint that runs the same
poll immediately — useful for local testing or verifying configuration without
waiting for the schedule:

```
POST /api/poll-now
```

It is secured like `/api/notify`: it requires the Function key (`?code=...` or
`x-functions-key` header) and, when `NOTIFY_API_KEY` is set, the matching
`x-api-key` header. It returns `{"status":"ok","alertsSent":<n>}`, or
`{"status":"disabled",...}` when no monitors are configured. Example against a
local `func start`:

```powershell
curl -X POST http://localhost:7071/api/poll-now
```

The query reads completed semantic-model queries (`OperationName == "QueryEnd"`)
using the standard `Timestamp`, `DurationMs`, `ItemId`, `ItemName`, `EventText`,
and `ExecutingUser` fields. Before enabling another Monitoring Eventhouse,
verify that it exposes the same schema:

```kql
.show table SemanticModelLogs schema as json
```

The bot app registration used by the Function App must be a Viewer in each
Fabric workspace being monitored. The tenant must allow service principals to
use Fabric APIs, and Graph `User.Read.All` application permission must have admin
consent. Direct Eventhouse queries use the
`https://kusto.kusto.windows.net/.default` token audience. The Teams app must also
be published in the organization catalog for cold-start delivery.

To configure an already deployed Function App without redeploying infrastructure:

```powershell
$monitors = @(
  @{
    name = "Production monitoring"
    workspaceId = "1fea49ee-a4a1-48ad-b431-431a9cb613df"
    kqlDatabaseId = "0f969a09-faaf-40a2-9269-890686804944"
    notifyModelOwner = $true
    durationMs = 30000
  }
) | ConvertTo-Json -Depth 4 -Compress

az functionapp config appsettings set `
  --resource-group rg-pbi_ops_agent `
  --name pbiops-func-zwxa2ieqj5e6q `
  --settings EVENTHOUSE_MONITORS_JSON=$monitors
```

Deploy the updated Python package after setting the configuration:

```powershell
./scripts/3-deploy-function.ps1 -FunctionAppName pbiops-func-zwxa2ieqj5e6q
```

## Prerequisites (must exist before you deploy)

The Bicep in `scripts/2` only provisions the **proactive bot layer** (Function App,
Azure Bot + Teams channel, Storage, App Insights/Log Analytics, VNet/private
endpoints, and the Function App's storage RBAC). Several things it depends on are
**not** created by this repo and must already exist — or be created via the steps
below — first:

| Prerequisite | Created by | Notes |
|---|---|---|
| **Azure AI Foundry** project/resource + published prompt agent | **You / your AI team — NOT this repo** | The agent (`FOUNDRY_AGENT_NAME`, default `data-architect-agent`) and its project endpoint (`FOUNDRY_PROJECT_ENDPOINT`) are *inputs* to Bicep. Provision the Foundry resource, project, agent, and any toolbox/skills separately in Azure AI Foundry. |
| **App registration + service principal** ("PBI Ops Bot") | `scripts/1-create-app-registration.ps1` (skip if you already have one) | One single-tenant app reg is reused for Bot auth, Microsoft Graph, Foundry data plane, Fabric APIs, and Power BI REST. Requires rights to create app regs **and** grant admin consent. |
| **Foundry RBAC** for the app registration | `scripts/4-foundry-role-and-teams-package.ps1` | Grants *Azure AI Developer* (+ *Cognitive Services User*) on the **existing** Foundry resource so the bot can call the agent. |
| **Teams app package (zip)** published to the org catalog | `scripts/4-...ps1` builds it; an **admin** uploads it | Required for proactive delivery (Graph auto-installs the app for target users). See "Manual steps that need an admin". |
| **Fabric-specific configuration** | **Manual, in Fabric/Power BI** | See "Fabric configuration" below — needed only for the slow-query alerts feature. |

> Only **one** service principal (the app registration) is yours to manage. The
> Function App also gets a **system-assigned managed identity** automatically
> (used only for Storage RBAC) — you don't create or manage that one.

### Fabric configuration (for slow-query alerts)

The Monitoring Eventhouse polling feature additionally requires (none of this is
provisioned by Bicep):

1. **A Fabric Monitoring Eventhouse** (Workspace Monitoring enabled) with the
   `SemanticModelLogs` table — this is the source of slow-query events.
2. **`EVENTHOUSE_MONITORS_JSON`** app setting pointing at it (workspace id +
   KQL database id). See "Automatic slow-query alerts".
3. **Workspace access for the app registration** — add "PBI Ops Bot" as at least
   a **Viewer** on each monitored workspace (owner resolution reads datasets/reports).
4. **Tenant admin settings** (Fabric Admin portal → *Developer settings*), enabled
   for the whole org or a group containing the app registration:
   - *Service principals can use Fabric APIs*
   - *Service principals can use Power BI APIs*

Without steps 3–4 every poll fails with 401/500 and no alerts are sent.

## Deploy

Prereqs: **Azure CLI**, **Azure Functions Core Tools v4**, and rights to create
an app registration + grant admin consent.

```powershell
az login
az account set --subscription "<your subscription>"

# 1) App registration + Graph permissions + admin consent
./scripts/1-create-app-registration.ps1      # prints botAppId / botAppTenantId / botAppPassword

# 2) Infrastructure
./scripts/2-deploy-infra.ps1 `
  -BotAppId <appId> -BotAppTenantId <tenantId> -BotAppPassword <secret> `
  -NotifyApiKey "<strong-random-string>"
# FoundryProjectEndpoint + FoundryAgentName default to the data-architect agent.
# note the functionAppName output

# 3) Publish the Python code
./scripts/3-deploy-function.ps1 -FunctionAppName <functionAppName>

# 4) Grant Foundry data-plane role + build Teams package
./scripts/4-foundry-role-and-teams-package.ps1 `
  -BotAppId <appId> -FoundryResourceId "<Foundry project/account resource id>"
```

## Manual steps that need an admin (org-level)

1. **Admin consent** for the Graph application permissions
   (`TeamsAppInstallation.ReadWriteForUser.All`, `AppCatalog.Read.All`,
   `Chat.ReadBasic.All`, `User.Read.All`) — step 1 attempts this; a Global/Privileged
   admin may need to approve in Entra ID.
2. **Teams app package**: add `color.png` (192×192) and `outline.png` (32×32) to
   `teams/`, set `botId` in `teams/manifest.json` to your `botAppId`, then upload
   the zip via **Teams Admin Center → Manage apps** (or side-load for testing).
   The manifest `id` must equal `TEAMS_APP_EXTERNAL_ID`.
3. **Foundry role**: the app registration needs *Azure AI Developer* on the
   Foundry project (step 4).
4. **Fabric/Power BI tenant settings** (only for slow-query alerts): in the
   **Fabric Admin portal → Developer settings**, enable *Service principals can
   use Fabric APIs* and *Service principals can use Power BI APIs* (org-wide or
   for a group containing the app registration), and add the app registration as
   a **Viewer** on each monitored workspace. See "Fabric configuration" above.

## Getting a user's AAD object id

```powershell
az ad user show --id "user@contoso.com" --query id -o tsv
```

## Local testing

Copy `src/local.settings.json.example` → `src/local.settings.json`, fill values,
then `cd src && func start`. Use the Bot Framework Emulator against
`http://localhost:7071/api/messages`. (Proactive cold-start needs real Teams +
Graph consent.)

## Security notes

- `/api/notify` is protected by both a Function key (`?code=`) **and** the
  `x-api-key` header (`NOTIFY_API_KEY`).
- Secrets are stored as Function App settings. For production, move
  `MicrosoftAppPassword` / `AZURE_CLIENT_SECRET` / `NOTIFY_API_KEY` to Key Vault
  references, and consider a user-assigned managed identity for Foundry access
  instead of the client secret.
