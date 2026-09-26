// NeuroDB v3 on Azure Container Apps.
//
// One image, two kinds of workload:
//   * <prefix>-web          the website (Container App, HTTPS ingress, 1..N replicas)
//   * <prefix>-<job>        management commands as Container Apps Jobs (schedules + manual runs)
//
// Deploy in two passes (docs/DEPLOYMENT_AZURE.md):
//   1. deployApps=false  -> registry, identity, Key Vault, storage, monitoring, environment
//   2. put the secrets in Key Vault and push the first image
//   3. deployApps=true   -> web app and jobs, reading secrets from Key Vault with the identity
//
// The PostgreSQL database is NOT created here: v3 attaches to the existing NeuroDB database
// (docs/DATA_MIGRATION.md). Its connection string is the Key Vault secret `database-url`.

targetScope = 'resourceGroup'

@description('Short prefix for every resource name, e.g. neurodb-prod.')
@maxLength(16)
param prefix string = 'neurodb-prod'

param location string = resourceGroup().location

@description('Pass 2: deploy the web app and the jobs (the secrets and the image must exist).')
param deployApps bool = false

@description('Full image reference, e.g. neurodbacr.azurecr.io/neurodb:3f2a1c9.')
param image string = ''

@description('Create the container registry (false = use an existing one in this resource group).')
param createRegistry bool = true
@minLength(5)
@maxLength(50)
param registryName string

@description('Create the Key Vault (false = use an existing RBAC-mode vault in this resource group).')
param createKeyVault bool = true
@maxLength(24)
param keyVaultName string

@description('Globally unique, 3-24 lowercase letters and digits.')
@maxLength(24)
param storageAccountName string

@description('Public host name, e.g. neuro-db.org. Empty until the custom domain is bound.')
param customDomain string = ''

@description('Optional subnet (/23 or larger, delegated to Microsoft.App/environments) for private database access.')
param infrastructureSubnetId string = ''

@description('Microsoft Entra ID single sign-on. When true the secret entra-client-secret must exist.')
param enableSso bool = false
param entraTenantId string = ''
param entraClientId string = ''

@description('eTools Datamart sync (basic auth). When true the secrets etools-username and etools-password must exist.')
param enableEtoolsDatamart bool = true

@description('Country filter of the eTools Datamart sync.')
param etoolsDatamartCountry string = 'Lebanon'

@description('AI assistant (Ask NeuroDB, OpenAI API). When true the secret openai-api-key must exist.')
param enableAiAssistant bool = false
@description('OpenAI model for the AI assistant.')
param aiAssistantModel string = 'gpt-5.5'
@description('Reasoning effort for the AI assistant: none, minimal, low, medium, high, xhigh or max (support varies by model).')
param aiAssistantEffort string = 'medium'

@description('Pages opened to the public without sign-in: any of library, maps, population.')
param publicPages array = []
param supportEmail string = ''
param userGuideUrl string = ''
param adminUrlPath string = 'manage/'
param timeZone string = 'Asia/Beirut'

@minValue(0)
param minReplicas int = 1
@minValue(1)
param maxReplicas int = 3

@description('Scheduled and manual jobs. cron is UTC (Beirut is UTC+3 in summer, UTC+2 in winter); empty = manual.')
param jobs array = [
  { name: 'migrate', cron: '', args: ['migrate'], cpu: '0.5', memory: '1Gi', timeout: 1800 }
  { name: 'ai-structure', cron: '', args: ['manage', 'import_activityinfo_structure', '--all', '--triggered-by', 'job'], cpu: '0.5', memory: '1Gi', timeout: 3600 }
  { name: 'ai-data', cron: '0 15 1-22 * *', args: ['manage', 'import_activityinfo_data', '--current-year', '--triggered-by', 'job'], cpu: '1.0', memory: '2Gi', timeout: 7200 }
  { name: 'etools', cron: '30 17 * * *', args: ['manage', 'sync_etools_datamart', '--triggered-by', 'job'], cpu: '0.5', memory: '1Gi', timeout: 7200 }
  { name: 'locations', cron: '0 2 * * *', args: ['manage', 'sync_locations', '--triggered-by', 'job'], cpu: '0.5', memory: '1Gi', timeout: 1800 }
  { name: 'daily-review', cron: '0 3 * * *', args: ['manage', 'daily_review', '--triggered-by', 'job'], cpu: '0.5', memory: '1Gi', timeout: 1800 }
  { name: 'freshness', cron: '15 * * * *', args: ['manage', 'check_sync_freshness'], cpu: '0.25', memory: '0.5Gi', timeout: 300 }
]

param tags object = { application: 'NeuroDB', owner: 'UNICEF Lebanon' }

// ------------------------------------------------------------------------------ built-in roles
var roleAcrPull = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var roleKeyVaultSecretsUser = '4633458b-17de-408a-b874-0445c86b69e6'
var roleStorageBlobDataContributor = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'

// ------------------------------------------------------------------------------ monitoring
resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${prefix}-logs'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${prefix}-insights'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logs.id
    DisableLocalAuth: false
  }
}

// ------------------------------------------------------------------------------ identity
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-identity'
  location: location
  tags: tags
}

// ------------------------------------------------------------------------------ registry
resource registryNew 'Microsoft.ContainerRegistry/registries@2023-07-01' = if (createRegistry) {
  name: registryName
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false
  }
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, registryName, identity.id, roleAcrPull)
  scope: registry
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleAcrPull)
  }
  dependsOn: [registryNew]
}

// ------------------------------------------------------------------------------ secrets
resource vaultNew 'Microsoft.KeyVault/vaults@2023-07-01' = if (createKeyVault) {
  name: keyVaultName
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    enablePurgeProtection: true
    publicNetworkAccess: 'Enabled'
  }
}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource vaultSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, keyVaultName, identity.id, roleKeyVaultSecretsUser)
  scope: vault
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleKeyVaultSecretsUser)
  }
  dependsOn: [vaultNew]
}

// ------------------------------------------------------------------------------ file storage
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  tags: tags
  sku: { name: 'Standard_ZRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false // the app uses its managed identity; no account keys anywhere
    accessTier: 'Hot'
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    deleteRetentionPolicy: { enabled: true, days: 30 }
    containerDeleteRetentionPolicy: { enabled: true, days: 30 }
  }
}

resource mediaContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'media'
  properties: { publicAccess: 'None' }
}

resource blobContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, identity.id, roleStorageBlobDataContributor)
  scope: storage
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleStorageBlobDataContributor)
  }
}

// ------------------------------------------------------------------------------ container environment
resource containerEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${prefix}-env'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
    vnetConfiguration: empty(infrastructureSubnetId) ? null : {
      infrastructureSubnetId: infrastructureSubnetId
      internal: false
    }
    zoneRedundant: false
  }
}

// ------------------------------------------------------------------------------ shared app settings
var kvSecretUri = 'https://${keyVaultName}${environment().suffixes.keyvaultDns}/secrets/'
var secretNames = concat(
  ['django-secret-key', 'database-url', 'activityinfo-token', 'etools-token'],
  enableSso ? ['entra-client-secret'] : [],
  enableEtoolsDatamart ? ['etools-username', 'etools-password'] : [],
  enableAiAssistant ? ['openai-api-key'] : []
)
var appSecrets = [for s in secretNames: {
  name: s
  keyVaultUrl: '${kvSecretUri}${s}'
  identity: identity.id
}]
var registries = [
  {
    server: '${registryName}.azurecr.io'
    identity: identity.id
  }
]
var hosts = empty(customDomain) ? '' : customDomain
var commonEnv = concat(
  [
    { name: 'DJANGO_ENV', value: 'production' }
    { name: 'DJANGO_SECRET_KEY', secretRef: 'django-secret-key' }
    { name: 'DATABASE_URL', secretRef: 'database-url' }
    { name: 'ACTIVITYINFO_TOKEN', secretRef: 'activityinfo-token' }
    { name: 'ETOOLS_TOKEN', secretRef: 'etools-token' }
    { name: 'ALLOWED_HOSTS', value: hosts }
    { name: 'CSRF_TRUSTED_ORIGINS', value: empty(customDomain) ? '' : 'https://${customDomain}' }
    { name: 'TIME_ZONE', value: timeZone }
    { name: 'LOG_FORMAT', value: 'json' }
    { name: 'AZURE_STORAGE_ACCOUNT', value: storage.name }
    { name: 'AZURE_CONTAINER_MEDIA', value: mediaContainer.name }
    { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
    { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
    { name: 'PUBLIC_PAGES', value: join(publicPages, ',') }
    { name: 'SUPPORT_EMAIL', value: supportEmail }
    { name: 'USER_GUIDE_URL', value: userGuideUrl }
    { name: 'ADMIN_URL_PATH', value: adminUrlPath }
  ],
  enableSso
    ? [
        { name: 'ENTRA_TENANT_ID', value: entraTenantId }
        { name: 'ENTRA_CLIENT_ID', value: entraClientId }
        { name: 'ENTRA_CLIENT_SECRET', secretRef: 'entra-client-secret' }
      ]
    : [],
  enableEtoolsDatamart
    ? [
        { name: 'ETOOLS_USERNAME', secretRef: 'etools-username' }
        { name: 'ETOOLS_PASSWORD', secretRef: 'etools-password' }
        { name: 'ETOOLS_DATAMART_COUNTRY', value: etoolsDatamartCountry }
      ]
    : [],
  enableAiAssistant
    ? [
        { name: 'OPENAI_API_KEY', secretRef: 'openai-api-key' }
        { name: 'AI_ASSISTANT_MODEL', value: aiAssistantModel }
        { name: 'AI_ASSISTANT_EFFORT', value: aiAssistantEffort }
      ]
    : []
)

// ------------------------------------------------------------------------------ web app
resource web 'Microsoft.App/containerApps@2024-03-01' = if (deployApps) {
  name: '${prefix}-web'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    managedEnvironmentId: containerEnv.id
    workloadProfileName: null
    configuration: {
      activeRevisionsMode: 'Single' // a new revision takes traffic only once its readiness probe passes
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
        traffic: [{ latestRevision: true, weight: 100 }]
      }
      registries: registries
      secrets: appSecrets
    }
    template: {
      containers: [
        {
          name: 'web'
          image: image
          args: ['web']
          env: concat(commonEnv, [{ name: 'WEB_CONCURRENCY', value: '3' }])
          resources: { cpu: json('0.5'), memory: '1Gi' }
          probes: [
            {
              type: 'Startup'
              httpGet: { path: '/healthz/live/', port: 8000 }
              periodSeconds: 3
              failureThreshold: 20
            }
            {
              type: 'Liveness'
              httpGet: { path: '/healthz/live/', port: 8000 }
              periodSeconds: 15
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: { path: '/healthz/', port: 8000 }
              periodSeconds: 15
              timeoutSeconds: 5
              failureThreshold: 3
            }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: [{ name: 'http', http: { metadata: { concurrentRequests: '40' } } }]
      }
    }
  }
  dependsOn: [acrPull, vaultSecretsUser, blobContributor]
}

// ------------------------------------------------------------------------------ jobs
resource containerJobs 'Microsoft.App/jobs@2024-03-01' = [for j in jobs: if (deployApps) {
  name: '${prefix}-${j.name}'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    environmentId: containerEnv.id
    configuration: {
      triggerType: empty(j.cron) ? 'Manual' : 'Schedule'
      manualTriggerConfig: empty(j.cron) ? { parallelism: 1, replicaCompletionCount: 1 } : null
      scheduleTriggerConfig: empty(j.cron) ? null : { cronExpression: j.cron, parallelism: 1, replicaCompletionCount: 1 }
      replicaTimeout: j.timeout
      replicaRetryLimit: 0 // a failed run stays visible as failed instead of being silently retried
      registries: registries
      secrets: appSecrets
    }
    template: {
      containers: [
        {
          name: j.name
          image: image
          args: j.args
          env: commonEnv
          resources: { cpu: json(j.cpu), memory: j.memory }
        }
      ]
    }
  }
  dependsOn: [acrPull, vaultSecretsUser, blobContributor]
}]

// ------------------------------------------------------------------------------ outputs
output registryLoginServer string = '${registryName}.azurecr.io'
output identityClientId string = identity.properties.clientId
output identityPrincipalId string = identity.properties.principalId
output environmentStaticIp string = containerEnv.properties.staticIp
output environmentDefaultDomain string = containerEnv.properties.defaultDomain
output webFqdn string = deployApps ? web!.properties.configuration.ingress.fqdn : ''
output keyVaultUri string = 'https://${keyVaultName}${environment().suffixes.keyvaultDns}/'
