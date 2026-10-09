$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

Describe "Start-PADOS-Web launcher" {
    It "keeps both services local and cleans selected ports" {
        $source = Get-Content -Raw -Encoding UTF8 (Join-Path $Root "Start-PADOS-Web.ps1")
        $source | Should Match "127\.0\.0\.1"
        $source | Should Match "Find-FreePort"
        $source | Should Match "Stop-PortListener"
        $source | Should Not Match "postgresql\+?psycopg://"
    }

    It "is reachable from the existing slash launcher" {
        $source = Get-Content -Raw -Encoding UTF8 (Join-Path $Root "Start-PADOS.ps1")
        $source | Should Match 'LauncherCommand -eq "/web"'
        $source | Should Match "Start-PADOS-Web\.ps1"
    }
}
