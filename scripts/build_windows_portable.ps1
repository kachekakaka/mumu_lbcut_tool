#requires -Version 7
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateRange(1, 99999)][int]$Candidate,
    [string]$EmbedZip,
    [string]$Wheelhouse,
    [string]$ToolsDirectory
)
$ErrorActionPreference = 'Stop'
$buildArgs = @('--candidate', $Candidate)
if ($EmbedZip) { $buildArgs += @('--embed-zip', $EmbedZip) }
if ($Wheelhouse) { $buildArgs += @('--wheelhouse', $Wheelhouse) }
if ($ToolsDirectory) { $buildArgs += @('--tools-dir', $ToolsDirectory) }
& py -3.11 -B -X utf8 (Join-Path $PSScriptRoot 'portable/build.py') @buildArgs
exit $LASTEXITCODE
