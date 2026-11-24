#requires -Version 7
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateRange(1, 99999)][int]$Candidate,
    [string]$EmbedZip,
    [string]$Wheelhouse,
    [string]$ToolsDirectory,
    [string]$UploadZip
)
$ErrorActionPreference = 'Stop'
$buildArgs = @('--candidate', $Candidate)
if ($EmbedZip) { $buildArgs += @('--embed-zip', $EmbedZip) }
if ($Wheelhouse) { $buildArgs += @('--wheelhouse', $Wheelhouse) }
if ($ToolsDirectory) { $buildArgs += @('--tools-dir', $ToolsDirectory) }
if ($UploadZip) { $buildArgs += @('--upload-zip', $UploadZip) }
& py -3.11 -B -X utf8 (Join-Path $PSScriptRoot 'portable/build.py') @buildArgs
exit $LASTEXITCODE
