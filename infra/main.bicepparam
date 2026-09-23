// Example parameters for production. No secrets here: they live in Key Vault (docs/DEPLOYMENT_AZURE.md).
using './main.bicep'

param prefix = 'neurodb-prod'
param registryName = 'neurodbacr'           // must be globally unique
param keyVaultName = 'neurodb-prod-kv'      // must be globally unique
param storageAccountName = 'neurodbprodfiles'  // must be globally unique, lowercase
param deployApps = false                    // pass 1; set true (or --parameters deployApps=true) for pass 2
param image = ''                            // e.g. neurodbacr.azurecr.io/neurodb:<git sha>
param customDomain = ''                     // e.g. neuro-db.org once DNS is ready
param enableSso = false
param entraTenantId = ''
param entraClientId = ''
param publicPages = []                      // e.g. ['library', 'maps']
param supportEmail = ''
param minReplicas = 1
param maxReplicas = 3
