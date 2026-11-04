$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
$logs = Join-Path $root "logs"
New-Item -ItemType Directory -Force $logs | Out-Null

$services = [ordered]@{
    "order-service"     = 45000
    "inventory-service" = 45001
    "promise-service"   = 45002
    "returns-service"   = 45003
    "api-gateway"       = 45004
}
$procs = @{}
$script:failures = 0

# Gateway talks to the services over IPv4 loopback
$env:ORDER_URL     = "http://127.0.0.1:45000"
$env:INVENTORY_URL = "http://127.0.0.1:45001"
$env:PROMISE_URL   = "http://127.0.0.1:45002"
$env:RETURNS_URL   = "http://127.0.0.1:45003"

$gw  = "http://127.0.0.1:45004"
$inv = "http://127.0.0.1:45001"
$ret = "http://127.0.0.1:45003"

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
        else {
            try {
                $reader = New-Object System.IO.StreamReader($resp.GetResponseStream())
                $text = $reader.ReadToEnd()
            } catch {}
        }
        $parsed = $null
        try { $parsed = $text | ConvertFrom-Json } catch {}
        return [pscustomobject]@{ Status = [int]$resp.StatusCode; Body = $parsed }
    }
}

function Stop-Tree($name) {
    if ($procs.ContainsKey($name) -and $procs[$name]) {
        $id = $procs[$name].Id
        cmd /c "taskkill /PID $id /T /F >nul 2>&1"
    }
}

function New-Checkout($sku, $qty) {
    return @{
        customer_id   = "C-DEMO"
        region        = "south"
        service_level = "standard"
        items         = @(@{ sku = $sku; qty = $qty })
    }
}

function Get-Reserved($sku) {
    return (Invoke-Api GET "$inv/stock/${sku}?region=south").Body.reserved
}

# Refuse to start if a port is already taken
foreach ($name in $services.Keys) {
    $busy = Get-NetTCPConnection -LocalPort $services[$name] -State Listen -ErrorAction SilentlyContinue
    if ($busy) { throw "Port $($services[$name]) is already in use ($name). Stop whatever is using it first." }
}

try {
    Write-Host "`n== Starting services ==" -ForegroundColor Cyan
    foreach ($name in $services.Keys) {
        $startArgs = @{
            FilePath               = $py
            WorkingDirectory       = (Join-Path $root "services\$name")
            ArgumentList           = @("-m", "uvicorn", "app.main:app", "--port", "$($services[$name])")
            RedirectStandardOutput = (Join-Path $logs "$name.out.log")
            RedirectStandardError  = (Join-Path $logs "$name.err.log")
            WindowStyle            = "Hidden"
            PassThru               = $true
        }
        $procs[$name] = Start-Process @startArgs
    }

    foreach ($name in $services.Keys) {
        $ready = $false
        for ($i = 0; $i -lt 40; $i++) {
            $r = Invoke-Api GET "http://127.0.0.1:$($services[$name])/health"
            if ($r.Status -eq 200) { $ready = $true; break }
            Start-Sleep -Milliseconds 500
        }
        Check "$name is up on port $($services[$name])" $ready
        if (-not $ready) { throw "$name failed to start. See logs\$name.err.log" }
    }

    Write-Host "`n== Dependency health ==" -ForegroundColor Cyan
    $h = Invoke-Api GET "$gw/health/deps"
    Check "gateway sees all four downstream services" ($h.Body.status -eq "ok")

    Write-Host "`n== Seed stock ==" -ForegroundColor Cyan
    Check "seed SKU-1 (10 units)" ((Invoke-Api PUT "$inv/stock/SKU-1" @{ region = "south"; on_hand = 10 }).Status -eq 200)
    Check "seed SKU-2 (2 units)" ((Invoke-Api PUT "$inv/stock/SKU-2" @{ region = "south"; on_hand = 2 }).Status -eq 200)

    Write-Host "`n== Happy-path checkout ==" -ForegroundColor Cyan
    $c1 = Invoke-Api POST "$gw/checkout" (New-Checkout "SKU-1" 2)
    Check "checkout returns 201" ($c1.Status -eq 201)
    Check "response has a delivery promise" ($null -ne $c1.Body.promise)
    Check "response is not degraded" ($c1.Body.degraded -eq $false)
    Check "2 units reserved" ((Get-Reserved "SKU-1") -eq 2)
    $orderId = $c1.Body.order.id
    Write-Host "  order $orderId, delivery $($c1.Body.promise.earliest_delivery) to $($c1.Body.promise.latest_delivery)"

    Write-Host "`n== Idempotent retry ==" -ForegroundColor Cyan
    $hdr = @{ "Idempotency-Key" = "demo-key-1" }
    $a = Invoke-Api POST "$gw/checkout" (New-Checkout "SKU-1" 1) $hdr
    $b = Invoke-Api POST "$gw/checkout" (New-Checkout "SKU-1" 1) $hdr
    Check "both calls succeed" (($a.Status -eq 201) -and ($b.Status -eq 201))
    Check "same order id returned" ($a.Body.order.id -eq $b.Body.order.id)
    Check "stock reserved only once (3 total)" ((Get-Reserved "SKU-1") -eq 3)

    Write-Host "`n== Cancel releases stock ==" -ForegroundColor Cyan
    $x = Invoke-Api POST "$gw/orders/$orderId/cancel"
    Check "cancel returns 200" ($x.Status -eq 200)
    Check "order is CANCELLED" ($x.Body.order.status -eq "CANCELLED")
    Check "stock_released is true" ($x.Body.stock_released -eq $true)
    Check "reserved back down to 1" ((Get-Reserved "SKU-1") -eq 1)

    Write-Host "`n== Out of stock rolls back ==" -ForegroundColor Cyan
    $o = Invoke-Api POST "$gw/checkout" (New-Checkout "SKU-2" 5)
    Check "checkout returns 409" ($o.Status -eq 409)
    Check "shortage reports 2 available" ($o.Body.detail.shortages[0].available -eq 2)
    Check "nothing left reserved for SKU-2" ((Get-Reserved "SKU-2") -eq 0)

    Write-Host "`n== Return through to refund ==" -ForegroundColor Cyan
    $today = Get-Date -Format "yyyy-MM-dd"
    $delivered = (Get-Date).AddDays(-3).ToString("yyyy-MM-dd")
    $rr = Invoke-Api POST "$gw/returns" @{
        order_id = $orderId; sku = "SKU-1"; qty = 2; unit_price_paise = 50000
        reason = "CHANGED_MIND"; delivered_on = $delivered; requested_on = $today
    }
    Check "return created via gateway" ($rr.Status -eq 201)
    Check "10% restocking fee applied (refund 90000 paise)" ($rr.Body.refund_paise -eq 90000)
    $retId = $rr.Body.id
    foreach ($act in "approve", "receive", "refund") {
        $t = Invoke-Api POST "$ret/returns/$retId/$act"
        Check "return $act" ($t.Status -eq 200)
    }
    Check "return ends REFUNDED" ((Invoke-Api GET "$ret/returns/$retId").Body.status -eq "REFUNDED")

    Write-Host "`n== Metrics ==" -ForegroundColor Cyan
    $m = (Invoke-WebRequest -UseBasicParsing "$gw/metrics").Content
    Check "gateway exposes checkout counters" ($m -match "gateway_checkouts_total")

    Write-Host "`n== Resilience: kill promise-service ==" -ForegroundColor Cyan
    Stop-Tree "promise-service"
    Start-Sleep -Seconds 1
    $d = Invoke-Api POST "$gw/checkout" (New-Checkout "SKU-1" 1)
    Check "checkout still succeeds (201)" ($d.Status -eq 201)
    Check "response marked degraded, promise null" (($d.Body.degraded -eq $true) -and ($null -eq $d.Body.promise))
    Check "deps health reports degraded" ((Invoke-Api GET "$gw/health/deps").Body.status -eq "degraded")

    Write-Host "`n== Resilience: kill inventory-service ==" -ForegroundColor Cyan
    Stop-Tree "inventory-service"
    Start-Sleep -Seconds 1
    $e = Invoke-Api POST "$gw/checkout" (New-Checkout "SKU-1" 1)
    Check "checkout fails cleanly with 503" ($e.Status -eq 503)
}
finally {
    Write-Host "`n== Stopping services ==" -ForegroundColor Cyan
    foreach ($name in $services.Keys) { Stop-Tree $name }
}

Write-Host ""
if ($script:failures -eq 0) {
    Write-Host "ALL CHECKS PASSED" -ForegroundColor Green
    exit 0
}
else {
    Write-Host "$($script:failures) CHECK(S) FAILED. See logs\ for service output." -ForegroundColor Red
    exit 1
}
