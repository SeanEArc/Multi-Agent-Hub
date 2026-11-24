# Microsoft Office through COM, for Multi-Agent Hub (revision 4, attachments PR 2). Prints ONE JSON line.
#   -Action check   -In <tiny .docx> -Out <pdf> [-ExcelOut <pdf>]   the test conversion: Word (and Excel) to PDF
#   -Action pdf     -In <docx> -Out <pdf>                            Word saves the document as a PDF
#   -Action compare -In <original> -Revised <edited> -Out <docx> -Author <name>
#                                                                    Word's Compare: a tracked-changes copy
# Office is never visible, alerts and macros are off, documents are opened read-only and never saved over. The hub
# runs this with a time limit and afterwards kills only Office processes automation started (app/doctool/office.py).
param(
    [Parameter(Mandatory)][ValidateSet('check', 'pdf', 'compare')][string]$Action,
    [string]$In,
    [string]$Out,
    [string]$ExcelOut,
    [string]$Revised,
    [string]$Author = 'Agent'
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$watch = [Diagnostics.Stopwatch]::StartNew()
$result = [ordered]@{ ok = $false }

function Release($object) {
    if ($null -ne $object) { try { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($object) } catch {} }
}

function Open-Word {
    $app = New-Object -ComObject Word.Application
    $app.Visible = $false
    $app.DisplayAlerts = 0                    # wdAlertsNone
    $app.AutomationSecurity = 3               # msoAutomationSecurityForceDisable: no macros
    $app.Options.ConfirmConversions = $false
    $app.Options.UpdateLinksAtOpen = $false    # linked files and pictures aren't fetched or refreshed
    $app.Options.UpdateLinksAtPrint = $false
    $app.Options.UpdateFieldsAtPrint = $false  # saving a PDF doesn't refresh fields (INCLUDETEXT, links)
    return $app
}

function Open-Document($app, $path) {
    # Documents.Open(FileName, ConfirmConversions=no, ReadOnly=yes, AddToRecentFiles=no). OpenAndRepair is off by
    # default and the application is hidden; the later optional arguments (Visible, OpenAndRepair,
    # NoEncodingDialog) can't be passed from PowerShell: COM's by-reference optional arguments refuse
    # [Type]::Missing mixed with values ("Cannot convert the True value of type bool to type Object", seen live).
    # COM has no switch against a remote attached template (\\host\share\x.dotm): Word may still try to fetch it
    # when the document opens. Known limit; macros stay off (AutomationSecurity 3) and links aren't updated.
    return $app.Documents.Open($path, $false, $true, $false)
}

$word = $null
$excel = $null
try {
    $word = Open-Word
    $result.version = [string]$word.Version
    $result.build = [string]$word.Build
    switch ($Action) {
        'check' {
            $doc = Open-Document $word $In
            $doc.ExportAsFixedFormat($Out, 17)   # wdExportFormatPDF
            $doc.Close(0)                        # wdDoNotSaveChanges
            Release $doc
            $result.word = (Test-Path -LiteralPath $Out)
            $result.excel = $false
            if ($ExcelOut) {
                try {
                    $excel = New-Object -ComObject Excel.Application
                    $excel.Visible = $false
                    $excel.DisplayAlerts = $false
                    $excel.AutomationSecurity = 3
                    $book = $excel.Workbooks.Add()
                    $book.Worksheets.Item(1).Range('A1').Value2 = 1
                    $book.ExportAsFixedFormat(0, $ExcelOut)   # xlTypePDF
                    $book.Close($false)
                    Release $book
                    $result.excel = (Test-Path -LiteralPath $ExcelOut)
                } catch {
                    $result.excel_error = $_.Exception.Message
                }
            }
            $result.ok = [bool]$result.word
        }
        'pdf' {
            $doc = Open-Document $word $In
            $doc.ExportAsFixedFormat($Out, 17)
            $result.pages = $doc.ComputeStatistics(2)   # wdStatisticPages
            $doc.Close(0)
            Release $doc
            $result.ok = (Test-Path -LiteralPath $Out)
        }
        'compare' {
            $original = Open-Document $word $In
            $edited = Open-Document $word $Revised
            # CompareDocuments(Original, Revised, Destination=new document, Granularity=word level, compare
            # formatting, case, whitespace, tables, headers, footnotes, text boxes, fields, comments, moves,
            # RevisedAuthor, IgnoreAllComparisonWarnings). RevisedAuthor names the agent (Application.UserName
            # doesn't: Word uses the signed-in account, probe 2026-11-21).
            $compared = $word.CompareDocuments($original, $edited, 2, 1, $true, $true, $true, $true, $true, $true,
                $true, $true, $true, $true, $Author, $true)
            $result.revisions = $compared.Revisions.Count
            $compared.SaveAs2($Out, 16)   # wdFormatXMLDocument
            $compared.Close(0)
            $original.Close(0)
            $edited.Close(0)
            Release $compared; Release $original; Release $edited
            $result.ok = (Test-Path -LiteralPath $Out)
        }
    }
} catch {
    $result.ok = $false
    $result.error = $_.Exception.Message
} finally {
    foreach ($app in @($excel, $word)) {
        if ($null -ne $app) { try { $app.Quit() } catch {}; Release $app }
    }
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}
$result.seconds = [math]::Round($watch.Elapsed.TotalSeconds, 2)
$result | ConvertTo-Json -Compress
