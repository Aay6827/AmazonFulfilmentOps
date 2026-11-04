param([switch]$Keep)
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$gw = "http://127.0.0.1:45004"
$script:failures = 0

function Check($name, $ok) {
    if ($ok) { Write-Host "  PASS  $name" -ForegroundColor Green }
    else { Write-Host "  FAIL  $name" -ForegroundColor Red; $script:failures++ }
}

function Invoke-Api($Method, $Url, $Body = $null, $Headers = @{}) {
    try {
        $params = @{ Method = $Method; Uri = $Url; UseBasicParsing = $true; Headers = $Headers }
        if ($null -ne $Body) {
            $params.Body = ConvertTo-Json -InputObject $Body -Depth 6
            $params.ContentType = "application/json"
        }
        $r = Invoke-WebRequest @params
        $parsed = $null
        try { $parsed = $r.Content | ConvertFrom-Json } catch {}
        return [pscustomobject]@{ Status = [int]$r.StatusCode; Body = $parsed }
    }
    catch {
        $resp = $_.Exception.Response
        if ($null -eq $resp) { return [pscustomobject]@{ Status = 0; Body = $null } }
        $text = ""
        if ($_.ErrorDetails -and $_.ErrorDetails.Message) { $text = $_.ErrorDetails.Message }
        $parsed = $null
        try { $parsed = $text | ConvertFrom-Json } catch {}
        return [pscustomobject]@{ Status = [int]$resp.StatusCode; Body = $parsed }
    }
}

function New-Checkout($sku, $qty) {
    return @{
        customer_id   = "C-DOCKER"
        region        = "south"
        service_level = "standard"
        items         = @(@{ sku = $sku; qty = $qty })
    }
}

docker info *> $null
if ($LASTEXITCODE -ne 0) { throw "Docker is not running. Start Docker Desktop and try again." }

$busy = Get-NetTCPConnection -LocalPort 45000,45001,45002,45003,45004 -State Listen -ErrorAction SilentlyContinue
if ($busy) { throw "Ports 45000-45004 are in use. Stop other servers or containers first." }

try {
    Write-Host "`n== Building and starting the stack ==" -ForegroundColor Cyan
    Write-Host "Building images one at a time, with retries..."
    $env:COMPOSE_PARALLEL_LIMIT = "1"
    $built = $false
    for ($try = 1; ($try -le 3) -and (-not $built); $try++) {
        docker compose build
        if ($LASTEXITCODE -eq 0) { $built = $true }
        else {
            Write-Host "Build attempt $try failed (network?). Retrying..." -ForegroundColor Yellow
            Start-Sleep -Seconds 5
        }
    }
    if (-not $built) { throw "docker compose build failed 3 times. Check your internet connection and try again." }
    docker compose up -d --wait
    if ($LASTEXITCODE -ne 0) { throw "docker compose up failed. Run 'docker compose logs' to investigate." }

    Write-Host "`n== Health ==" -ForegroundColor Cyan
    $h = Invoke-Api GET "$gw/health/deps"
    Check "gateway sees all four services" ($h.Body.status -eq "ok")

    Write-Host "`n== Network exposure and hardening ==" -ForegroundColor Cyan
    $exposed = Get-NetTCPConnection -LocalPort 45000,45001,45002,45003 -State Listen -ErrorAction SilentlyContinue
    Check "internal services are NOT published on the host" ($null -eq $exposed)
    Check "gateway is published on 45004" ((Invoke-Api GET "$gw/health").Status -eq 200)
    $uid = (docker compose exec -T order-service id -u | Out-String).Trim()
    Check "containers run as non-root (uid 10001)" ($uid -eq "10001")

    Write-Host "`n== Seed stock ==" -ForegroundColor Cyan
    $seed = Get-Content scripts\seed_stock.py -Raw | docker compose exec -T inventory-service python -
    Check "stock seeded (SKU-1: 10, SKU-2: 2)" ((($seed | Out-String).Trim() -split "\s+") -join "," -eq "200,200")

    Write-Host "`n== Checkout flows ==" -ForegroundColor Cyan
    $c = Invoke-Api POST "$gw/checkout" (New-Checkout "SKU-1" 2)
    Check "checkout returns 201 with a promise" (($c.Status -eq 201) -and ($null -ne $c.Body.promise))
    $hdr = @{ "Idempotency-Key" = "docker-key-1" }
    $a = Invoke-Api POST "$gw/checkout" (New-Checkout "SKU-1" 1) $hdr
    $b = Invoke-Api POST "$gw/checkout" (New-Checkout "SKU-1" 1) $hdr
    Check "idempotent retry returns the same order" (($a.Status -eq 201) -and ($a.Body.order.id -eq $b.Body.order.id))
    $o = Invoke-Api POST "$gw/checkout" (New-Checkout "SKU-2" 5)
    Check "out of stock gives 409 (2 available)" (($o.Status -eq 409) -and ($o.Body.detail.shortages[0].available -eq 2))
    $o2 = Invoke-Api POST "$gw/checkout" (New-Checkout "SKU-2" 2)
    Check "failed attempt reserved nothing (SKU-2 x2 still succeeds)" ($o2.Status -eq 201)

    Write-Host "`n== Returns ==" -ForegroundColor Cyan
    $today = Get-Date -Format "yyyy-MM-dd"
    $delivered = (Get-Date).AddDays(-3).ToString("yyyy-MM-dd")
    $rr = Invoke-Api POST "$gw/returns" @{
        order_id = "ORD-DOCKER"; sku = "SKU-1"; qty = 2; unit_price_paise = 50000
        reason = "CHANGED_MIND"; delivered_on = $delivered; requested_on = $today
    }
    Check "return created, 10% fee applied (90000 paise)" (($rr.Status -eq 201) -and ($rr.Body.refund_paise -eq 90000))

    Write-Host "`n== Resilience: stop promise-service container ==" -ForegroundColor Cyan
    docker compose stop promise-service 2>&1 | Out-Null
    $d = Invoke-Api POST "$gw/checkout" (New-Checkout "SKU-1" 1)
    Check "checkout degrades gracefully" (($d.Status -eq 201) -and ($d.Body.degraded -eq $true))

    Write-Host "`n== Resilience: stop inventory-service container ==" -ForegroundColor Cyan
    docker compose stop inventory-service 2>&1 | Out-Null
    $e = Invoke-Api POST "$gw/checkout" (New-Checkout "SKU-1" 1)
    Check "checkout fails cleanly with 503" ($e.Status -eq 503)

    Write-Host "`n== Image sizes ==" -ForegroundColor Cyan
    docker image ls "amazonfulfilmentops/*"
}
finally {
    if ($Keep) {
        Write-Host "`nStack left running (-Keep). Stop it with: docker compose down" -ForegroundColor Yellow
    }
    else {
        Write-Host "`n== Tearing down ==" -ForegroundColor Cyan
        docker compose down --remove-orphans 2>&1 | Out-Null
    }
}

Write-Host ""
if ($script:failures -eq 0) { Write-Host "ALL CHECKS PASSED" -ForegroundColor Green; exit 0 }
else { Write-Host "$($script:failures) CHECK(S) FAILED" -ForegroundColor Red; exit 1 }
