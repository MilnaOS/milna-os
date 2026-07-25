# Only run between 10pm and 6am
$hour = (Get-Date).Hour
if ($hour -ge 6 -and $hour -lt 22) { exit 0 }

$corpusPath  = "D:\Runtime\kb\battletech\corpus.txt"
$visitedPath = "D:\Runtime\kb\battletech\corpus.visited"

function Show-Toast($title, $msg) {
    [void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
    $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
        [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
    $template.SelectSingleNode('//text[@id=1]').InnerText = $title
    $template.SelectSingleNode('//text[@id=2]').InnerText = $msg
    $toast = [Windows.UI.Notifications.ToastNotification]::new($template)
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Milna Scrape').Show($toast)
}

# Check if scrape task is running
$task = Get-ScheduledTask -TaskPath '\Milna\' -TaskName 'Milna-BattleTech-NightScrape' -ErrorAction SilentlyContinue
if ($task -and $task.State -ne 'Running') {
    Show-Toast 'Milna Scrape — STOPPED' "Task is not running (state: $($task.State)). corpus.visited has $(if (Test-Path $visitedPath) { (Get-Content $visitedPath | Measure-Object -Line).Lines } else { 0 }) URLs."
    exit 1
}

# Check corpus.visited is being written (stall detection — no new URLs in 30 min)
if (Test-Path $visitedPath) {
    $age = (Get-Date) - (Get-Item $visitedPath).LastWriteTime
    if ($age.TotalMinutes -gt 30) {
        $lines = (Get-Content $visitedPath | Measure-Object -Line).Lines
        Show-Toast 'Milna Scrape — STALLED' "No new URLs written in $([int]$age.TotalMinutes) min. $lines URLs visited so far."
        exit 1
    }
}

exit 0
