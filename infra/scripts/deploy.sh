#!/usr/bin/env bash
# Roll one image out to NeuroDB on Azure Container Apps:
#   1. migrations (the migrate job, with the new image)   -> stops here if they fail
#   2. new web revision (Single revision mode: traffic moves only when its readiness probe passes)
#   3. every scheduled/manual job switched to the new image
#   4. smoke test: /healthz/ on the public host reports the new version
#
# Usage (Azure CLI logged in, containerapp extension installed):
#   RESOURCE_GROUP=rg-neurodb-prod PREFIX=neurodb-prod \
#   IMAGE=neurodbacr.azurecr.io/neurodb:3f2a1c9 infra/scripts/deploy.sh
set -euo pipefail

: "${RESOURCE_GROUP:?set RESOURCE_GROUP}"
: "${PREFIX:?set PREFIX (the Bicep prefix, e.g. neurodb-prod)}"
: "${IMAGE:?set IMAGE (registry/repository:tag)}"
JOBS="${JOBS:-ai-structure ai-data etools locations freshness daily-review}"
VERSION="${IMAGE##*:}"
SMOKE_HOST="${SMOKE_HOST:-}"   # custom domain; defaults to the Container Apps host name

step() { printf '\n==> %s\n' "$*"; }

step "Migrations with $IMAGE"
az containerapp job update --name "$PREFIX-migrate" --resource-group "$RESOURCE_GROUP" --image "$IMAGE" --output none
execution=$(az containerapp job start --name "$PREFIX-migrate" --resource-group "$RESOURCE_GROUP" --query name --output tsv)
status=""
for _ in $(seq 1 120); do
  status=$(az containerapp job execution show --name "$PREFIX-migrate" --resource-group "$RESOURCE_GROUP" \
    --job-execution-name "$execution" --query properties.status --output tsv)
  case "$status" in
    Succeeded) break ;;
    Failed | Stopped | Degraded)
      echo "Migration run $execution ended as $status. Nothing else was changed."
      echo "Logs: az containerapp job logs show -n $PREFIX-migrate -g $RESOURCE_GROUP --execution $execution --container migrate"
      exit 1 ;;
  esac
  sleep 10
done
[ "$status" = "Succeeded" ] || { echo "Migration run $execution did not finish in 20 minutes."; exit 1; }

step "Web revision"
suffix="v$(printf '%s' "$VERSION" | tr -cd 'a-z0-9' | cut -c1-10)-$(date +%m%d%H%M)"
az containerapp update --name "$PREFIX-web" --resource-group "$RESOURCE_GROUP" --image "$IMAGE" \
  --revision-suffix "$suffix" --output none
host="${SMOKE_HOST:-$(az containerapp show --name "$PREFIX-web" --resource-group "$RESOURCE_GROUP" \
  --query properties.configuration.ingress.fqdn --output tsv)}"

step "Jobs"
for job in $JOBS; do
  az containerapp job update --name "$PREFIX-$job" --resource-group "$RESOURCE_GROUP" --image "$IMAGE" --output none
  echo "  $PREFIX-$job -> $VERSION"
done

step "Smoke test https://$host/healthz/"
for _ in $(seq 1 36); do
  body=$(curl --silent --show-error --fail "https://$host/healthz/" || true)
  if printf '%s' "$body" | grep -q "\"version\": \"$VERSION\""; then
    echo "$body"
    echo "Deployed $VERSION."
    exit 0
  fi
  sleep 10
done
echo "The new revision did not report version $VERSION within 6 minutes; the previous revision keeps serving."
echo "Inspect: az containerapp revision list -n $PREFIX-web -g $RESOURCE_GROUP -o table"
exit 1
