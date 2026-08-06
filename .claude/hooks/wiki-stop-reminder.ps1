# Stop hook - gently reminds to update the LLM-Wiki when code/infra/docs changed
# but docs/wiki/ did not. No blocking (decision is never set) - a nudge only.
# Noise filter: silent on chat-only turns; silent when the wiki was touched.
# Anti-loop: exits immediately when stop_hook_active=true.
# Design: docs/wiki/conventions.md section 6.
#
# Output: only { systemMessage, suppressOutput } -- the Stop schema has no
# hookSpecificOutput variant, so additionalContext is NOT valid here.
# ASCII-only source (see wiki-session-start.ps1 for why); the message text lives
# in wiki-stop-reminder.msg.txt (UTF-8). Stdout is forced to UTF-8.

$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false

# Read the hook-input JSON from process stdin.
$raw = [Console]::In.ReadToEnd()
$data = $null
if ($raw) { try { $data = $raw | ConvertFrom-Json } catch { $data = $null } }

# Anti-loop: if a Stop hook is already active/blocking, allow the stop.
if ($data -and $data.stop_hook_active) { exit 0 }

$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

Push-Location $root
# -uall: expand untracked directories into individual files (otherwise a brand-new
# directory shows up as a single '?? dir/' line and no pattern below matches).
$status = & git status --porcelain -uall 2>$null
Pop-Location

if (-not $status) { exit 0 }

$lines = @($status -split "`n" | Where-Object { $_ -ne '' })

# porcelain: 'XY <path>'; rename -> 'orig -> new'. git always emits forward slashes.
$paths = foreach ($l in $lines) {
    if ($l.Length -le 3) { continue }
    $p = $l.Substring(3)
    if ($p -match ' -> ') { $p = ($p -split ' -> ')[-1] }
    $p.Trim().Trim('"')
}

$watched = @($paths | Where-Object {
    $_ -like 'src/*' -or $_ -like 'tests/*' -or $_ -like 'deploy/*' -or
    $_ -like '.github/*' -or $_ -like 'docs/superpowers/*' -or
    $_ -eq 'Dockerfile' -or $_ -eq 'docker-compose.yml' -or $_ -eq 'Caddyfile'
})
$wikiTouched = @($paths | Where-Object { $_ -like 'docs/wiki/*' })

if ($watched.Count -eq 0 -or $wikiTouched.Count -gt 0) { exit 0 }

$msgPath = Join-Path $PSScriptRoot 'wiki-stop-reminder.msg.txt'
if (-not (Test-Path $msgPath)) { exit 0 }

$msg = (Get-Content -Path $msgPath -Raw -Encoding UTF8).Trim()
if (-not $msg) { exit 0 }

# The Stop output schema has NO hookSpecificOutput variant; systemMessage is the
# channel for a non-blocking nudge (reason only reaches the model with decision=block).
$out = @{
    systemMessage  = $msg
    suppressOutput = $true
}
$out | ConvertTo-Json -Depth 5 -Compress
exit 0
