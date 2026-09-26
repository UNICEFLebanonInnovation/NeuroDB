#!/usr/bin/env bash
# Put a previous image back on the web app and the jobs (database migrations are not reversed).
# Usage: RESOURCE_GROUP=rg PREFIX=neurodb-prod IMAGE=neurodbacr.azurecr.io/neurodb:<previous tag> infra/scripts/rollback.sh
set -euo pipefail
: "${RESOURCE_GROUP:?}" "${PREFIX:?}" "${IMAGE:?}"
JOBS="${JOBS:-migrate ai-structure ai-data etools locations freshness daily-review}"
az containerapp update --name "$PREFIX-web" --resource-group "$RESOURCE_GROUP" --image "$IMAGE" --output none
for job in $JOBS; do
  az containerapp job update --name "$PREFIX-$job" --resource-group "$RESOURCE_GROUP" --image "$IMAGE" --output none
done
echo "Web app and jobs now run $IMAGE."
