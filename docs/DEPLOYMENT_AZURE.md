# Deploying NeuroDB v3 to Azure (Docker container)

NeuroDB ships as **one Docker image**. On Azure it runs on **Azure Container Apps**:

```
                 HTTPS (managed certificate, custom domain)
Users ───────────────────────────────▶  neurodb-prod-web   (Container App, 1–3 replicas)
                                              │   image: <acr>/neurodb:<git sha>
                                              │   probes: /healthz/live/ and /healthz/
                                              ▼
   Container Apps Jobs (same image)     Existing NeuroDB PostgreSQL (unchanged schema, TLS)
   ├─ neurodb-prod-migrate       manual        ▲
   ├─ neurodb-prod-ai-structure  manual        │  DATABASE_URL, tokens, keys from Key Vault
   ├─ neurodb-prod-ai-data       15:00 UTC, days 1–22     (user-assigned managed identity)
   ├─ neurodb-prod-etools        17:30 UTC
   ├─ neurodb-prod-locations     02:00 UTC                Blob storage (identity, no keys)
   ├─ neurodb-prod-freshness     hourly                   Log Analytics + Application Insights
   └─ neurodb-prod-daily-review  03:00 UTC
```

Everything is described in `infra/main.bicep`. The database is **not** created: v3 attaches to
the existing NeuroDB database and only adds three tables (`docs/DATA_MIGRATION.md`).

## The image

| Command | What it runs |
|---|---|
| `docker build --build-arg APP_VERSION=$(git rev-parse --short HEAD) -t neurodb .` | Build (about 40 s) |
| `docker run --env-file .env -p 8000:8000 neurodb` | Pending migrations, then the website (gunicorn, port `$PORT`, default 8000) |
| `docker run --env-file .env neurodb migrate` | Migrations, then default roles |
| `docker run --env-file .env neurodb manage sync_etools` | Any management command |
| `docker run --env-file .env neurodb check` | Django deployment checks |

Properties that matter in Azure:

- Runs as a non-root user (uid 10001). Code and libraries are read-only for that user, and the
  container also works with a read-only root filesystem.
- Static files are collected at build time and served by WhiteNoise with compression and
  one-year cache headers. There is no separate static host.
- `/healthz/live/` answers without touching the database (liveness, startup). `/healthz/` checks
  the database and reports the last successful run of each sync (readiness). It returns 503 when the
  database is unreachable, so traffic stays on the previous revision. Both answer before host
  validation and the HTTPS redirect, so the platform's internal probes work.
- Every start of the website applies pending migrations first, under a PostgreSQL advisory lock so
  that parallel containers never migrate at the same time. A failed migration stops the container
  instead of serving a half-migrated schema. `RUN_MIGRATIONS=false` turns this off.
- `SIGTERM` stops it in about a second. Logs go to stdout as JSON (`LOG_FORMAT=json`).
- No test tools inside: the image installs `requirements.lock` only; `requirements-dev.lock` is for
  developers and CI.
- `BASE_IMAGE` defaults to `python:3.12-slim-bookworm`. If Docker Hub rate-limits the build, import
  that tag into your registry once (`az acr import --name <acr> --source docker.io/library/python:3.12-slim-bookworm`)
  and build with `--build-arg BASE_IMAGE=<acr>.azurecr.io/library/python:3.12-slim-bookworm`.

## First deployment

Prerequisites: Azure CLI 2.60+ with `az extension add --name containerapp`, Owner (or Contributor
plus User Access Administrator) on the resource group, and the connection details of the existing
NeuroDB database.

### 1. Resource group and names

Edit `infra/main.bicepparam`. The registry, Key Vault and storage account names must be globally
unique.

```bash
az group create --name rg-neurodb-prod --location westeurope
```

### 2. Pass 1: platform resources

```bash
az deployment group create --resource-group rg-neurodb-prod \
  --template-file infra/main.bicep --parameters infra/main.bicepparam
```

This creates the registry, managed identity (with pull, secret-read and blob roles), Key Vault
(RBAC mode, purge protection), storage account with a private `media` container, Log Analytics,
Application Insights and the Container Apps environment. No application runs yet.

### 3. Secrets in Key Vault

Give yourself **Key Vault Secrets Officer** on the vault, then set the secrets. Values are read from
files or generated so they never appear in shell history.

| Secret | Value |
|---|---|
| `django-secret-key` | 64 random characters |
| `database-url` | `postgres://<user>:<password>@<server>.postgres.database.azure.com:5432/<database>?sslmode=require` |
| `activityinfo-token` | ActivityInfo service-account API token |
| `etools-token` | eTools REST token, only for the on-demand `sync_etools` (can stay empty) |
| `etools-username` | eTools Datamart service account (basic authentication user name) |
| `etools-password` | eTools Datamart password; required while `enableEtoolsDatamart = true` (the default) |
| `entra-client-secret` | only when single sign-on is enabled (step 8) |
| `openai-api-key` | only when the AI assistant is enabled (`enableAiAssistant = true`): the OpenAI project API key (see `docs/OPERATIONS.md`) |

```bash
KV=neurodb-prod-kv
az keyvault secret set --vault-name $KV --name django-secret-key \
  --value "$(python3 -c 'import secrets; print(secrets.token_urlsafe(64))')" --output none
az keyvault secret set --vault-name $KV --name database-url --file ./database-url.txt --output none
az keyvault secret set --vault-name $KV --name activityinfo-token --file ./activityinfo-token.txt --output none
az keyvault secret set --vault-name $KV --name etools-token --file ./etools-token.txt --output none
az keyvault secret set --vault-name $KV --name etools-username --file ./etools-username.txt --output none
az keyvault secret set --vault-name $KV --name etools-password --file ./etools-password.txt --output none
shred -u ./database-url.txt ./activityinfo-token.txt ./etools-token.txt ./etools-username.txt ./etools-password.txt
```

Use a dedicated database login for the application, with rights on the existing tables and
permission to create the three new `core_*` tables. Do not reuse the v2 credentials; the review found
them committed to the old repository.

### 4. Database network access

The container environment must reach the existing PostgreSQL server. Pick one:

- **Private access (recommended).** Create a subnet of at least `/23`, delegated to
  `Microsoft.App/environments`, in the virtual network that can reach the server (VNet-integrated
  Flexible Server or a private endpoint). Pass its id as `infrastructureSubnetId` in pass 1. The
  environment cannot be moved into a VNet later, so decide before pass 1.
- **Public access.** Keep the server public and add a firewall rule for the environment's outbound
  IP addresses. On a consumption environment without a VNet these can change; use this only for
  staging or with a NAT gateway on a VNet.

TLS is required either way; production settings default to `sslmode=require`.

### 5. First image

Build in Azure (no local Docker needed):

```bash
TAG=$(git rev-parse --short HEAD)
az acr build --registry neurodbacr --image neurodb:$TAG --build-arg APP_VERSION=$TAG .
```

### 6. Pass 2: web app and jobs

```bash
az deployment group create --resource-group rg-neurodb-prod \
  --template-file infra/main.bicep --parameters infra/main.bicepparam \
  --parameters deployApps=true image=neurodbacr.azurecr.io/neurodb:$TAG
```

Then apply the migrations. On the existing database this only creates the three `core_*` tables:

```bash
az containerapp job start --name neurodb-prod-migrate --resource-group rg-neurodb-prod
az containerapp job execution list --name neurodb-prod-migrate --resource-group rg-neurodb-prod --output table
```

The deployment output `webFqdn` is the temporary address
(`neurodb-prod-web.<id>.<region>.azurecontainerapps.io`). It is trusted automatically, before any
custom domain exists. Open `/healthz/`, then sign in.

Rehearse the whole sequence on a **restored copy** of the production database in a staging
resource group (`prefix=neurodb-stg`) before pointing production at the live database.

### 7. Custom domain (for example neuro-db.org)

```bash
az containerapp hostname add --hostname neuro-db.org --name neurodb-prod-web --resource-group rg-neurodb-prod
# create the DNS records the command prints (TXT asuid.<domain> and a CNAME or A record), then:
az containerapp hostname bind --hostname neuro-db.org --name neurodb-prod-web --resource-group rg-neurodb-prod \
  --environment neurodb-prod-env --validation-method CNAME
```

Set `customDomain = 'neuro-db.org'` in the parameters and run pass 2 again, so `ALLOWED_HOSTS` and
`CSRF_TRUSTED_ORIGINS` include the domain. The certificate is managed and renewed by Azure.

### 8. Single sign-on (Microsoft Entra ID)

1. App registration, single tenant, web redirect URI
   `https://<your host>/accounts/microsoft/login/callback/` (add the `azurecontainerapps.io` host
   too while testing).
2. Create a client secret and store it as `entra-client-secret` in Key Vault.
3. Set `enableSso = true`, `entraTenantId`, `entraClientId` and run pass 2 again.

Only users that already exist in NeuroDB can sign in; the email of the Entra account is matched
to the existing user.

## Environment variables

`infra/env/` lists every setting the application reads:

| File | Use |
|---|---|
| `azure-app-settings.json` | Paste into **Environment variables → App settings → Advanced edit** (App Service), or adapt for any tool that takes name/value pairs. Secrets are Key Vault references. |
| `containerapp-env.json` | The `secrets` and `env` blocks for a Container App or job (already generated by `infra/main.bicep`). |
| `azure-env-variables.json` | Catalogue: group, required, secret, Key Vault secret name and description of each variable. |

Replace every `<placeholder>`, the Key Vault name (`neurodb-prod-kv`) and the domain. Do not add
`CONTAINER_APP_NAME`, `CONTAINER_APP_ENV_DNS_SUFFIX`, `WEBSITE_HOSTNAME` or `APP_VERSION`: Azure or
the image build sets them. `AZURE_STORAGE_KEY` stays unset because storage access uses the managed identity.

## Pipeline (Azure DevOps)

`azure-pipelines.yml` runs on every pull request and on `main`:

1. **Verify:** lint, migration drift, tests against PostgreSQL 16, deployment checks and a
   dependency audit. It also builds the image, runs `check` inside it and waits for `/healthz/live/`.
2. **Image** (main only): build and push `<acr>/neurodb:<commit sha>`.
3. **Staging**, then **Production** (behind the approval on the `neurodb-production`
   environment): `infra/scripts/deploy.sh`.

`deploy.sh` runs the migrate job with the new image and stops if it fails; nothing else changes
in that case. Otherwise it rolls out a new web revision, switches every job to the new image, and
waits until `/healthz/` on the public host reports the new version.

One-time setup: an Azure Resource Manager service connection (workload identity federation) with
Contributor on both resource groups, a Docker Registry service connection to the ACR, and the two
environments. Put their names in the variables at the top of the pipeline.

## Day-to-day operations

| Task | Command |
|---|---|
| Follow web logs | `az containerapp logs show -n neurodb-prod-web -g rg-neurodb-prod --follow` |
| Run a sync now | `az containerapp job start -n neurodb-prod-etools -g rg-neurodb-prod` |
| Job history | `az containerapp job execution list -n neurodb-prod-ai-data -g rg-neurodb-prod -o table` |
| Job run logs | `az containerapp job logs show -n neurodb-prod-ai-data -g rg-neurodb-prod --execution <name> --container ai-data` |
| One-off command | `az containerapp exec -n neurodb-prod-web -g rg-neurodb-prod --command "neurodb manage createsuperuser"` |
| Roll back | `RESOURCE_GROUP=… PREFIX=neurodb-prod IMAGE=<acr>/neurodb:<previous sha> infra/scripts/rollback.sh` |
| Rotate a secret | Update it in Key Vault, then `az containerapp revision restart -n neurodb-prod-web -g … --revision <active>`; jobs read it on their next run |
| Scale | `minReplicas` and `maxReplicas` parameters (HTTP rule: 40 concurrent requests per replica) |

Schedules are cron expressions in **UTC** (Container Apps has no time zone setting). Beirut is
UTC+3 in summer and UTC+2 in winter, so local times move by an hour twice a year; adjust the `jobs`
parameter if exact local times matter. A failed job run stays failed (`replicaRetryLimit: 0`) and
is visible in the execution history and on the NeuroDB **Data health** page. The hourly
`freshness` job exits with an error when a source is stale. Create an Azure Monitor alert on failed
executions of the jobs (portal: the job → Alerts), notifying the NeuroDB team.

Rollback does not reverse migrations. Every migration must therefore stay compatible with the
previous release for one deployment (add columns first, remove them in a later release).

## Also possible: App Service for Containers

The same image runs on Web App for Containers: set `WEBSITES_PORT=8000`, the same environment
variables (Key Vault references), and health check path `/healthz/`. App Service WebJobs cannot run
this image's commands, so the scheduled syncs would still need Container Apps Jobs. That is why
Container Apps is the recommended target.
