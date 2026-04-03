Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.SetOutputToWaveFile('C:\Users\ahpea\cobot\eas_bot\test_tom_script.wav')
$synth.SelectVoice('ScanSoft Tom_Full_22kHz')
$synth.Speak('Hello from ScanSoft Tom.')
$synth.Dispose()