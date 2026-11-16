# Execute only against mocked cmdlets. No NetSecurity module or native firewall calls.
param([Parameter(Mandatory=$true)][string]$ScriptPath)
$ErrorActionPreference='Stop'
$global:rules=@{};$global:created=0
function Get-NetFirewallRule { param($Name,$ErrorAction);if($global:rules.ContainsKey($Name)){$global:rules[$Name]} }
function New-NetFirewallRule {
 param($Name,$DisplayName,$Group,$Direction,$Action,$Enabled,$Profile,$Protocol,$LocalPort)
 $global:created++
 $global:rules[$Name]=[pscustomobject]@{Name=$Name;Group=$Group;Direction=$Direction;Action=$Action;Enabled=$Enabled;Profile=$Profile;PolicyStoreSourceType='Local';Port=$LocalPort}
}
function Get-NetFirewallPortFilter {param([Parameter(ValueFromPipeline=$true)]$InputObject);process{[pscustomobject]@{Protocol='TCP';LocalPort=$InputObject.Port;RemotePort='Any'}}}
function Get-NetFirewallAddressFilter {param([Parameter(ValueFromPipeline=$true)]$InputObject);process{[pscustomobject]@{LocalAddress='Any';RemoteAddress='Any'}}}
function Get-NetFirewallApplicationFilter {param([Parameter(ValueFromPipeline=$true)]$InputObject);process{[pscustomobject]@{Program='Any'}}}
function Get-NetFirewallServiceFilter {param([Parameter(ValueFromPipeline=$true)]$InputObject);process{[pscustomobject]@{Service='Any'}}}
. $ScriptPath
if($global:created -ne 3){throw 'Expected three rules'}
. $ScriptPath
if($global:created -ne 3){throw 'Duplicate rules'}
$global:rules['AIImpulsHTTPS-TLS'].Group='Foreign'
$rejected=$false
try{. $ScriptPath}catch{$rejected=$true}
if(-not $rejected -or $global:created -ne 3){throw 'Foreign rule was not rejected'}
Write-Output 'PASS: fresh rules, repeated rules, foreign rule rejection (mock cmdlets only)'
