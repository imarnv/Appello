#!/bin/bash
# ─── Appello Bridge — Azure Deploy Script ──────────────────────────────
# Usage: ./deploy.sh
# This script deploys the bridge to Azure App Service and ensures all
# environment variables and settings are properly configured.
# Run this instead of `az webapp up` directly.
# ─────────────────────────────────────────────────────────────────────────

set -e

# ─── Config ─────────────────────────────────────────────────────────────
# Appello deploys to its OWN App Service. It shares the existing plan, which
# costs nothing extra, but is a separate app so a deploy here cannot disturb
# anything else already running on that plan.
#
# Do not point APP_NAME at another product's app. This tree has diverged from
# the bridge it was copied from — it drops the /ws/measurement routes and adds
# /webhooks/phonepe — so deploying it over a different service would quietly
# remove endpoints that service's callers still use.
APP_NAME="${APPELLO_APP_NAME:-appello-bridge}"
RESOURCE_GROUP="${APPELLO_RESOURCE_GROUP:-voicera}"
PLAN="${APPELLO_PLAN:-ASP-voicera-9892}"
SKU="B1"
RUNTIME="PYTHON:3.12"
STARTUP_CMD="./start.sh"

if [ "$APP_NAME" = "voicera-bridge" ]; then
  echo "❌ APP_NAME is set to the shared bridge. This tree would remove routes"
  echo "   that other apps still call. Deploy that service from its own folder."
  exit 1
fi

# ─── Step 0: create the app if this is the first deploy ────────────────────
if ! az webapp show --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
     --output none 2>/dev/null; then
  echo "📦 Step 0/3: '$APP_NAME' does not exist yet — creating it on plan $PLAN..."
  az webapp create \
    --name "$APP_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --plan "$PLAN" \
    --runtime "$RUNTIME" \
    --output none
  # WebSockets are off by default and this service is nothing but WebSockets.
  az webapp config set \
    --name "$APP_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --web-sockets-enabled true \
    --output none
  echo "✅ Created $APP_NAME with WebSockets enabled."
fi

echo "🚀 Deploying $APP_NAME to Azure..."

# ─── Step 1/3: Set all environment variables from .env ─────────────────────
echo "🔑 Step 1/3: Configuring environment variables..."

# Read .env file and build settings string
SETTINGS="SCM_DO_BUILD_DURING_DEPLOYMENT=true"
while IFS= read -r line || [ -n "$line" ]; do
  # Skip comments and empty lines
  [[ -z "$line" || "$line" =~ ^# ]] && continue
  # Remove surrounding quotes from value
  key="${line%%=*}"
  value="${line#*=}"
  value="${value%\"}"
  value="${value#\"}"
  # Skip PORT (Azure manages this)
  [[ "$key" == "PORT" ]] && continue
  SETTINGS="$SETTINGS $key=\"$value\""
done < .env

# Apply all settings at once
eval az webapp config appsettings set \
  --name "$APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --settings $SETTINGS \
  --output none

echo "✅ Environment variables and build configuration set."

# ─── Step 2/3: Set startup command ─────────────────────────────────────────
echo "⚙️  Step 2/3: Setting startup command..."
az webapp config set \
  --name "$APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --startup-file "$STARTUP_CMD" \
  --output none

echo "✅ Startup command set: $STARTUP_CMD"

# ─── Step 3/3: Package and deploy zip ──────────────────────────────────────
echo "📦 Step 3/3: Creating deployment package..."
rm -f ./bridge-deploy.zip
zip -q -r ./bridge-deploy.zip . -x ".git/*" "__pycache__/*" "*.pyc" ".env" ".env.*" "extracted_logs/*" "extracted_logs_fresh/*" "azure_logs/*" "*.zip" "*.log" "venv/*" ".venv/*" ".mypy_cache/*"

echo "🚀 Uploading package with build automation..."
az webapp deploy \
  --name "$APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --src-path ./bridge-deploy.zip \
  --type zip \
  --timeout 600

rm -f ./bridge-deploy.zip
echo "✅ Code upload complete."

# ─── Step 4: Restart to pick up new config ───────────────────────────────
echo "🔄 Restarting app..."
az webapp restart --name "$APP_NAME" --resource-group "$RESOURCE_GROUP"

echo ""
echo "════════════════════════════════════════════════════════════"
echo "  ✅ Deployment complete!"
echo "  🌐 URL: https://$(az webapp show --name \"$APP_NAME\" --resource-group \"$RESOURCE_GROUP\" --query defaultHostName -o tsv)"
echo "  📋 Startup: $STARTUP_CMD"
echo "════════════════════════════════════════════════════════════"
