# generate-proposal.ps1 - Hermes Freelance Stack automation
# Phase 15 deliverable. Generates a client proposal document
# by orchestrating: Gemini (draft) -> ClickUp (track) -> file (output).
#
# Prerequisites (all verified Phase 0):
#   - Gemini API key in GEMINI_API_KEY env var (or Hermes .env)
#   - ClickUp token in CLICKUP_TOKEN env var
#   - Node.js v24 (for clickup.js wrapper)
#   - git (for version control)
#
# Usage:
#   .\generate-proposal.ps1 -ClientName "Acme Corp" -Service "Web App Development" -Budget "$5,000" [-OutputFile "proposal.docx"]
#
# Deliberately NOT a one-shot black box. Each stage logs what it did
# and what it got back so Hermes can inspect, retry, or hand off.

param(
    [Parameter(Mandatory=$true)]
    [string]$ClientName,

    [Parameter(Mandatory=$true)]
    [string]$Service,

    [Parameter(Mandatory=$false)]
    [string]$Budget = "TBD",

    [Parameter(Mandatory=$false)]
    [string]$OutputFile = "proposal.md",

    [Parameter(Mandatory=$false)]
    [switch]$SkipClickUp
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$Stage = @{ n = 0 }
function Write-Stage {
    $Stage.n++
    Write-Host "`n[STAGE $($Stage.n)] $args" -ForegroundColor Cyan
}

# Use absolute path based on script location
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$root = $scriptDir  # automation/ is the root for relative paths
$evidenceDir = Join-Path $root "evidence"
if (!(Test-Path $evidenceDir)) { New-Item -ItemType Directory -Path $evidenceDir -Force | Out-Null }
$timestamp = Get-Date -Format "yyyy-MM-ddTHH-mm-ssZ"

Write-Host "`n============================================================"
Write-Host "  HERMES FREELANCE STACK - Proposal Automation"
Write-Host "  Client: $ClientName | Service: $Service | Budget: $Budget"
Write-Host "  Timestamp: $timestamp"
Write-Host "============================================================`n"

# STAGE 1: Validate prerequisites
Write-Stage "Validate prerequisites"
$missing = @()
if (-not $env:GEMINI_API_KEY) { $missing += "GEMINI_API_KEY" }
if (-not $env:CLICKUP_TOKEN -and -not $SkipClickUp) { $missing += "CLICKUP_TOKEN" }
if ($missing) {
    Write-Error "Missing env vars: $($missing -join ', '). Aborting."
    exit 1
}
Write-Host "  GEMINI_API_KEY  : $($env:GEMINI_API_KEY.Substring(0, [Math]::Min(8, $env:GEMINI_API_KEY.Length)))..."
if (-not $SkipClickUp) {
    Write-Host "  CLICKUP_TOKEN   : $($env:CLICKUP_TOKEN.Substring(0, [Math]::Min(8, $env:CLICKUP_TOKEN.Length)))..."
}
Write-Host "  Prerequisites OK"

# STAGE 2: Generate proposal draft via Gemini API
Write-Stage "Generate proposal draft via Gemini API (gemini-3.6-flash)"

$prompt = @"
You are a freelance software engineer preparing a client proposal.

CLIENT: $ClientName
SERVICE: $Service
BUDGET: $Budget

Write a professional, concise client proposal (max 2 pages / ~800 words).
Structure:
1. Executive Summary (2-3 sentences)
2. Scope of Work (deliverables, approach, timeline in weeks)
3. Investment (breakdown if budget is a range; otherwise state the figure)
4. Terms (payment schedule, timeline, what is NOT included)
5. Next Steps (call to action)

Tone: confident, clear, no buzzwords. Write as if the client is technical
but busy. Do NOT mention "AI" or "LLM" in the proposal text itself.
"@

$body = @{
    contents = @(
        @{ parts = @{ text = $prompt } }
    )
} | ConvertTo-Json -Depth 5

$geminiUrl = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent?key=$($env:GEMINI_API_KEY)"
try {
    $response = Invoke-RestMethod -Uri $geminiUrl -Method Post -Body $body -ContentType "application/json" -TimeoutSec 60
    $draft = $response.candidates[0].content.parts[0].text
    Write-Host "  Gemini draft generated ($($draft.Length) chars)"
} catch {
    Write-Error "Gemini API call failed: $($_.Exception.Message)"
    exit 1
}

# STAGE 3: Save draft to evidence folder
Write-Stage "Save draft to evidence folder"
$clientSlug = $ClientName -replace '\s+', '-'
$draftPath = Join-Path $evidenceDir "proposal-draft-$clientSlug-$timestamp.md"
$draft | Out-File -FilePath $draftPath -Encoding UTF8
Write-Host "  Draft saved: $draftPath"

# STAGE 4: Create ClickUp task to track proposal
if (-not $SkipClickUp) {
    Write-Stage "Create ClickUp task to track proposal (list=Projects, space=Freelance)"

    $listId = "1200430000004209"  # Projects list in Freelance space
    $safeName = "$ClientName - $Service Proposal"
    $clickUpBody = @{
        name        = $safeName
        description = "Auto-generated proposal draft. Client: $ClientName. Service: $Service. Budget: $Budget. Evidence: $draftPath"
        priority    = 2  # normal
        status      = "to do"
    } | ConvertTo-Json -Depth 3

    $clickUpUrl = "https://api.clickup.com/api/v2/list/$listId/task"
    try {
        $clickUpResp = Invoke-RestMethod -Uri $clickUpUrl -Method Post -Body $clickUpBody -ContentType "application/json" -TimeoutSec 15 -Headers @{ Authorization = $env:CLICKUP_TOKEN }
        $taskId = $clickUpResp.id
        Write-Host "  ClickUp task created: ID=$taskId, name='$safeName'"
        Write-Host "  Task URL: https://app.clickup.com/t/$taskId"
    } catch {
        Write-Warning "ClickUp task creation failed (non-blocking): $($_.Exception.Message)"
    }
} else {
    Write-Stage "SkipClickUp - skipping ClickUp task creation"
}

# STAGE 5: Write final proposal markdown
Write-Stage "Write final proposal markdown to $OutputFile"
if (Test-Path $OutputFile) {
    $outputPath = $OutputFile
} elseif (Test-Path (Join-Path $root $OutputFile)) {
    $outputPath = Join-Path $root $OutputFile
} else {
    $outputPath = $OutputFile  # use as-is (absolute or relative to CWD)
}
@"
# Proposal: $Service for $ClientName

**Prepared by:** Juma Moya (Freelance Software Engineer)
**Date:** $(Get-Date -Format "yyyy-MM-dd")
**Status:** Draft - pending client review

---

$draft

---

*Generated by Hermes Freelance Stack - proposal automation.*
*Evidence saved to: $draftPath*
"@ | Out-File -FilePath $outputPath -Encoding UTF8
Write-Host "  Final proposal written: $outputPath"

Write-Host "`n============================================================"
Write-Host "  DONE. Proposal generated for $ClientName."
Write-Host "  Evidence: $draftPath"
Write-Host "  Output  : $outputPath"
if (-not $SkipClickUp) { Write-Host "  Tracked : ClickUp task (see above)" }
Write-Host "============================================================`n"
