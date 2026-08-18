"""Configuration loaded from environment / Function App settings.

The same object is passed to ``ConfigurationBotFrameworkAuthentication`` (which
reads the ``APP_*`` attributes) and used throughout the app.

App code authenticates to Foundry / Graph / Table Storage as the bot app
registration via an explicit ``ClientSecretCredential`` built from the ``APP_*``
settings. We deliberately do NOT use ``AZURE_CLIENT_ID`` / ``AZURE_CLIENT_SECRET``
environment variables, because the Functions host would also consume them and
authenticate ``AzureWebJobsStorage`` as the service principal instead of its
system-assigned managed identity.
"""
import os

from azure.identity.aio import ClientSecretCredential


class Config:
    # --- Bot Framework auth (read by ConfigurationBotFrameworkAuthentication) ---
    APP_ID = os.environ.get("MicrosoftAppId", "")
    APP_PASSWORD = os.environ.get("MicrosoftAppPassword", "")
    APP_TYPE = os.environ.get("MicrosoftAppType", "SingleTenant")
    APP_TENANTID = os.environ.get("MicrosoftAppTenantId", "")

    # --- Azure AI Foundry (prompt agent via Responses API) ---
    # Project endpoint, e.g. https://<res>.services.ai.azure.com/api/projects/<project>
    FOUNDRY_PROJECT_ENDPOINT = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "")
    # The hosted agent name (agent_reference), NOT an asst_ id.
    FOUNDRY_AGENT_NAME = os.environ.get("FOUNDRY_AGENT_NAME", "")
    # Project toolbox (preview) that bundles the MCP tool + skills.
    TOOLBOX_NAME = os.environ.get("TOOLBOX_NAME", "toolbox")
    # Entra token audience for the Foundry data plane.
    FOUNDRY_TOKEN_SCOPE = os.environ.get(
        "FOUNDRY_TOKEN_SCOPE", "https://ai.azure.com/.default"
    )

    # --- State storage (Azure Table Storage, identity-based / RBAC) ---
    STORAGE_ACCOUNT_NAME = os.environ.get("STORAGE_ACCOUNT_NAME", "")

    # --- Teams / proactive messaging ---
    # externalId of the Teams app in the org catalog (== manifest "id" GUID).
    TEAMS_APP_EXTERNAL_ID = os.environ.get("TEAMS_APP_EXTERNAL_ID", "")
    # Regional Teams service URL used for cold-start proactive conversations.
    # amer / emea / apac etc. Overwritten automatically once a real activity is seen.
    SERVICE_URL_DEFAULT = os.environ.get(
        "TEAMS_SERVICE_URL_DEFAULT", "https://smba.trafficmanager.net/amer/"
    )

    # --- Security for the /api/notify trigger (in addition to the Function key) ---
    NOTIFY_API_KEY = os.environ.get("NOTIFY_API_KEY", "")

    # --- Monitoring Eventhouse slow-query alerts ---
    EVENTHOUSE_MONITORS_JSON = os.environ.get("EVENTHOUSE_MONITORS_JSON", "")
    QUERY_ALERT_POLL_SCHEDULE = os.environ.get(
        "QUERY_ALERT_POLL_SCHEDULE", "0 * * * * *"
    )
    QUERY_ALERT_DURATION_MS = int(os.environ.get("QUERY_ALERT_DURATION_MS", "30000"))
    QUERY_ALERT_LOOKBACK_MINUTES = int(
        os.environ.get("QUERY_ALERT_LOOKBACK_MINUTES", "5")
    )
    QUERY_ALERT_COOLDOWN_SECONDS = int(
        os.environ.get("QUERY_ALERT_COOLDOWN_SECONDS", "900")
    )
    QUERY_ALERT_MAX_ROWS = int(os.environ.get("QUERY_ALERT_MAX_ROWS", "100"))

    # --- Bot Framework OAuth connection used to get the user's Fabric token ---
    OAUTH_CONNECTION_NAME = os.environ.get("OAUTH_CONNECTION_NAME", "fabric")

    # --- Delegated Fabric scopes requested via the device-code flow ---
    FABRIC_SCOPES = os.environ.get(
        "FABRIC_SCOPES",
        " ".join(
            [
                "https://api.fabric.microsoft.com/Item.Read.All",
                "https://api.fabric.microsoft.com/Item.Execute.All",
                "https://api.fabric.microsoft.com/Dataset.Read.All",
                "https://api.fabric.microsoft.com/Dataset.ReadWrite.All",
                "https://api.fabric.microsoft.com/Report.Read.All",
                "https://api.fabric.microsoft.com/SemanticModel.Read.All",
                "https://api.fabric.microsoft.com/SemanticModel.Execute.All",
                "https://api.fabric.microsoft.com/Workspace.Read.All",
                "offline_access",
            ]
        ),
    )


def app_credential() -> ClientSecretCredential:
    """Credential for the bot app registration (Foundry / Graph / Tables)."""
    return ClientSecretCredential(
        tenant_id=Config.APP_TENANTID,
        client_id=Config.APP_ID,
        client_secret=Config.APP_PASSWORD,
    )
