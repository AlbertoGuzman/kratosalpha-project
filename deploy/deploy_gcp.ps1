# Despliegue en Google Cloud (PowerShell nativo): equivalente a deploy_gcp.sh.
#
# Uso (desde la raíz del repo):
#   .\deploy\deploy_gcp.ps1 -ProjectId kratosalpha-project-pre -Bucket kratosalpha-project-pre -Env pre
#
# Secretos esperados en Secret Manager: kratos-<env>-<clave>
#   alpaca-api-key, alpaca-secret-key (obligatorios)
#   github-token, github-repo, telegram-token, telegram-chat-id (opcionales)

param(
    [Parameter(Mandatory = $true)][string]$ProjectId,
    [Parameter(Mandatory = $true)][string]$Bucket,
    [ValidateSet("dev", "pre", "pro")][string]$Env = "pre",
    [string]$Region = "europe-west1",
    [string]$ArRepo = "trading",
    [string]$SaName = "trading-runner",
    [string]$SchedSaName = "trading-scheduler"
)

$ErrorActionPreference = "Continue"   # los fallos de gcloud se comprueban con $LASTEXITCODE
$Root = Split-Path -Parent $PSScriptRoot

$SaEmail      = "$SaName@$ProjectId.iam.gserviceaccount.com"
$SchedSaEmail = "$SchedSaName@$ProjectId.iam.gserviceaccount.com"
$Image        = "$Region-docker.pkg.dev/$ProjectId/$ArRepo/trading:latest"

function Paso([string]$m) { Write-Host "`n▶ $m" -ForegroundColor Cyan }

# Ejecuta gcloud y aborta el script si falla.
function G {
    & gcloud @args
    if ($LASTEXITCODE -ne 0) { throw "gcloud falló: gcloud $($args -join ' ')" }
}

# True si el comando gcloud termina con éxito (sin mostrar salida).
function Existe {
    & gcloud @args 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

Write-Host "Proyecto=$ProjectId región=$Region env=$Env bucket=$Bucket"
G config set project $ProjectId

Paso "APIs"
G services enable run.googleapis.com cloudscheduler.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com storage.googleapis.com

Paso "Artifact Registry y bucket"
if (-not (Existe artifacts repositories describe $ArRepo --location $Region)) {
    G artifacts repositories create $ArRepo --repository-format=docker --location $Region
}
if (-not (Existe storage buckets describe "gs://$Bucket")) {
    G storage buckets create "gs://$Bucket" --location $Region --uniform-bucket-level-access
}

Paso "Cuentas de servicio y permisos"
foreach ($sa in @($SaName, $SchedSaName)) {
    if (-not (Existe iam service-accounts describe "$sa@$ProjectId.iam.gserviceaccount.com")) {
        G iam service-accounts create $sa
    }
}
G storage buckets add-iam-policy-binding "gs://$Bucket" --member "serviceAccount:$SaEmail" --role roles/storage.objectAdmin | Out-Null
G projects add-iam-policy-binding $ProjectId --member "serviceAccount:$SaEmail" --role roles/secretmanager.secretAccessor --condition=None | Out-Null

# Proyectos nuevos: Cloud Build corre como la cuenta de Compute por defecto,
# que necesita permisos para construir y subir la imagen.
$ProjectNumber = (& gcloud projects describe $ProjectId --format="value(projectNumber)").Trim()
$BuildSa = "$ProjectNumber-compute@developer.gserviceaccount.com"
foreach ($rol in @("roles/cloudbuild.builds.builder", "roles/artifactregistry.writer", "roles/storage.objectViewer", "roles/logging.logWriter")) {
    G projects add-iam-policy-binding $ProjectId --member "serviceAccount:$BuildSa" --role $rol --condition=None | Out-Null
}

Paso "Secretos"
$mapa = [ordered]@{
    ALPACA_API_KEY    = "alpaca-api-key"
    ALPACA_SECRET_KEY = "alpaca-secret-key"
    GITHUB_TOKEN      = "github-token"
    GITHUB_REPO       = "github-repo"
    TELEGRAM_TOKEN    = "telegram-token"
    TELEGRAM_CHAT_ID  = "telegram-chat-id"
}
$secretos = @()
foreach ($var in $mapa.Keys) {
    $nombre = "kratos-$Env-$($mapa[$var])"
    if (Existe secrets describe $nombre) {
        $secretos += "$var=${nombre}:latest"
    } else {
        Write-Host "  · secreto $nombre no existe — se omite $var" -ForegroundColor Yellow
    }
}
$secretosArg = $secretos -join ","
if ($secretosArg -notmatch "ALPACA_API_KEY" -or $secretosArg -notmatch "ALPACA_SECRET_KEY") {
    throw "Faltan los secretos de Alpaca (kratos-$Env-alpaca-*)"
}
$githubFlag = if ($secretosArg -match "GITHUB_TOKEN" -and $secretosArg -match "GITHUB_REPO") { "true" } else { "false" }

Paso "Build de la imagen (5-10 min)"
G builds submit $Root --tag $Image --project $ProjectId

# max-retries=0 y tasks=1: un reintento automático podría duplicar órdenes.
function Desplegar-Job([string]$modo, [string]$cron, [string]$timeout) {
    $job = "trading-$Env-$modo"
    Paso "Job $job"
    G run jobs deploy $job --image $Image --region $Region --service-account $SaEmail `
        --tasks 1 --parallelism 1 --max-retries 0 --task-timeout $timeout --cpu 1 --memory 2Gi `
        --set-env-vars "TRADING_ENV=$Env,TRADING_RUN=$modo,GCS_BUCKET_NAME=$Bucket,GITHUB_ENABLED=$githubFlag" `
        --set-secrets $secretosArg

    G run jobs add-iam-policy-binding $job --region $Region --member "serviceAccount:$SchedSaEmail" --role roles/run.invoker | Out-Null

    $uri  = "https://run.googleapis.com/v2/projects/$ProjectId/locations/$Region/jobs/${job}:run"
    $verbo = if (Existe scheduler jobs describe $job --location $Region) { "update" } else { "create" }
    G scheduler jobs $verbo http $job --location $Region --schedule $cron --time-zone "America/New_York" `
        --uri $uri --http-method POST --oauth-service-account-email $SchedSaEmail
    Write-Host "  ✓ $job  ($cron America/New_York)" -ForegroundColor Green
}

Desplegar-Job "all"   "0 5 * * 1-5"   "30m"   # 05:00 ET
Desplegar-Job "exits" "45 15 * * 1-5" "15m"   # 14:45 ET (deadline MOC 15:50 ET)

Write-Host "`n✓ Despliegue completado." -ForegroundColor Green
Write-Host "  Prueba manual: gcloud run jobs execute trading-$Env-exits --region $Region --wait"
