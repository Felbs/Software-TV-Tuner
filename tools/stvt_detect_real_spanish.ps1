# Auto-classify every captured spa-tagged WAV in tools/data/spanish_hunt as
# "actually English" or "non-English (likely real Spanish)" using Windows'
# built-in English speech recognition.
#
# How it works
# - Build a System.Speech recognizer with the en-US dictation grammar
# - Feed each WAV in
# - Recognized text length is a proxy: if the recognizer returns lots of
#   confident English words, the audio is English (DVS narration, mis-
#   labelled track, etc.). If it returns little or nothing, the audio is
#   probably Spanish or other non-English.
#
# Outputs a summary table + a candidates list of WAVs likely to be real
# Spanish, which the user can play to confirm.

Add-Type -AssemblyName System.Speech

$SAMPLES_DIR = "Z:\src\magic-tv-decoder\tools\data\spanish_hunt"
$REPORT = "$SAMPLES_DIR\language_detect.txt"

$wavs = Get-ChildItem -Path $SAMPLES_DIR -Filter "*.wav" `
    -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match "_(spa|mul)\.wav$" } |
    Sort-Object Name

if (-not $wavs) {
    Write-Host "No spa/mul WAVs found in $SAMPLES_DIR" -ForegroundColor Red
    Write-Host "Run python tools/stvt_spanish_hunt.py first."
    exit 1
}

Write-Host ""
Write-Host ("Classifying {0} WAVs..." -f $wavs.Count)
Write-Host ""

$FFMPEG = "C:\ffmpeg\bin\ffmpeg.exe"

function Get-MeanVolume($path) {
    $err = & $FFMPEG -hide_banner -i $path -af volumedetect -f null - 2>&1
    foreach ($line in $err) {
        if ($line -match "mean_volume:\s*(-?[\d\.]+)\s*dB") {
            return [double]$matches[1]
        }
    }
    return -999.0
}

$results = @()
foreach ($wav in $wavs) {
    $rec = $null
    try {
        $rec = New-Object System.Speech.Recognition.SpeechRecognitionEngine
        $g = New-Object System.Speech.Recognition.DictationGrammar
        $rec.LoadGrammar($g)
        $rec.SetInputToWaveFile($wav.FullName)

        $allText = ""
        $totalConf = 0.0
        $nRecs = 0

        # Recognize() returns the next chunk; loop until end-of-file.
        for ($i = 0; $i -lt 25; $i++) {
            $r = $null
            try {
                $r = $rec.Recognize([TimeSpan]::FromSeconds(2))
            } catch {
                break
            }
            if ($null -eq $r) { break }
            $allText += $r.Text + " "
            $totalConf += $r.Confidence
            $nRecs += 1
        }

        $avgConf = if ($nRecs -gt 0) { $totalConf / $nRecs } else { 0 }
        $wordCount = ($allText -split "\s+" | Where-Object { $_ }).Count
        $mean_db = Get-MeanVolume $wav.FullName

        # Decision tree:
        #   silent (<-60 dB)                                 -> EMPTY
        #   audible + many English words at high conf        -> ENGLISH
        #   audible + 0-1 English words                      -> REAL SPANISH
        #   audible + middle ground                          -> UNCERTAIN
        $verdict = if ($mean_db -lt -60) {
                       "EMPTY (broadcaster sending silence)"
                   } elseif ($wordCount -ge 6 -and $avgConf -ge 0.3) {
                       "ENGLISH (probable DVS / mislabeled)"
                   } elseif ($wordCount -le 1) {
                       "REAL SPANISH (audible + no English)"
                   } else {
                       "UNCERTAIN"
                   }

        $results += [pscustomobject]@{
            File     = $wav.Name
            Mean_dB  = [math]::Round($mean_db, 1)
            Words    = $wordCount
            AvgConf  = [math]::Round($avgConf, 2)
            Verdict  = $verdict
            Excerpt  = if ($allText.Length -gt 60) {
                           $allText.Substring(0, 60).Trim() + "..."
                       } else { $allText.Trim() }
        }
        Write-Host ("{0,-35}  vol={1,6} dB  words={2,3}  conf={3,4}  {4}" `
            -f $wav.Name, [math]::Round($mean_db,1), $wordCount, `
               [math]::Round($avgConf,2), $verdict)
    } catch {
        Write-Host ("{0,-35}  ERROR: {1}" -f $wav.Name, $_.Exception.Message) `
            -ForegroundColor Yellow
    } finally {
        if ($rec) { $rec.Dispose() }
    }
}

# Final summary
Write-Host ""
Write-Host "=============== SUMMARY ===============" -ForegroundColor Cyan
$real = $results | Where-Object { $_.Verdict -like "REAL SPANISH*" }
Write-Host ("Real Spanish candidates: {0}" -f $real.Count) `
    -ForegroundColor Green
foreach ($c in $real) {
    Write-Host ("  {0}  vol={1} dB" -f $c.File, $c.Mean_dB) -ForegroundColor Green
}
Write-Host ""
$empty = $results | Where-Object { $_.Verdict -like "EMPTY*" }
Write-Host ("EMPTY (silent broadcaster) tracks: {0}" -f $empty.Count) `
    -ForegroundColor DarkYellow
foreach ($e in $empty) {
    Write-Host ("  {0}  vol={1} dB" -f $e.File, $e.Mean_dB) -ForegroundColor DarkYellow
}
Write-Host ""
$mislabel = $results | Where-Object { $_.Verdict -like "ENGLISH*" }
Write-Host ("English (DVS / mislabel) tracks: {0}" -f $mislabel.Count) `
    -ForegroundColor Yellow
foreach ($m in $mislabel) {
    Write-Host ("  {0}  vol={1} dB  excerpt: {2}" -f $m.File, $m.Mean_dB, $m.Excerpt) `
        -ForegroundColor Yellow
}
Write-Host ""

# Save full report
$results | Format-Table -AutoSize | Out-String | Set-Content -Path $REPORT
Write-Host ("Full report: {0}" -f $REPORT)
