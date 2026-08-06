# SessionStart hook - injects the project's LLM-Wiki into the session context.
# Reads docs/wiki/{index,overview}.md + the tail of log.md and returns them via
# hookSpecificOutput.additionalContext. Design: docs/wiki/decisions/0001.
#
# ASCII-only source on purpose: Windows PowerShell 5.1 parses a .ps1 as ANSI when
# it has no BOM, so non-ASCII in source breaks parsing. All human-language text
# lives in companion .txt files read at runtime as UTF-8. Stdout is forced to UTF-8.

$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false

# Repo root = two levels up from .claude/hooks/
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$wiki = Join-Path $root 'docs\wiki'

$parts = New-Object System.Collections.Generic.List[string]

function Add-Section($label, $path, $tail) {
    if (-not (Test-Path $path)) { return }
    if ($tail -gt 0) {
        $content = (Get-Content -Path $path -Tail $tail -Encoding UTF8) -join "`n"
    } else {
        $content = Get-Content -Path $path -Raw -Encoding UTF8
    }
    if ($content) { $script:parts.Add("===== $label =====`n$content") }
}

Add-Section 'docs/wiki/index.md'        (Join-Path $wiki 'index.md')    0
Add-Section 'docs/wiki/overview.md'     (Join-Path $wiki 'overview.md') 0
Add-Section 'docs/wiki/log.md (recent)' (Join-Path $wiki 'log.md')      40

if ($parts.Count -eq 0) { exit 0 }

$headerPath = Join-Path $PSScriptRoot 'wiki-session-start.header.txt'
$header = ''
if (Test-Path $headerPath) { $header = Get-Content -Path $headerPath -Raw -Encoding UTF8 }

$context = ($header + "`n`n" + ($parts -join "`n`n")).Trim()

$out = @{
    hookSpecificOutput = @{
        hookEventName     = 'SessionStart'
        additionalContext = $context
    }
}

$out | ConvertTo-Json -Depth 5 -Compress
exit 0
