param([string]$Title)
Add-Type -AssemblyName PresentationFramework
Write-Output "WPF fixture started"
[xml]$xaml = @"
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Width="400" Height="220">
    <StackPanel Margin="16">
        <TextBox Name="InputBox" AutomationProperties.Name="PCBridge input" />
        <Button Name="ApplyButton" AutomationProperties.Name="PCBridge apply">Apply</Button>
        <TextBlock Name="Status" Text="Ready" />
        <PasswordBox Name="Pass" />
    </StackPanel>
</Window>
"@
$window = [Windows.Markup.XamlReader]::Load((New-Object System.Xml.XmlNodeReader $xaml))
$window.Title = $Title
$inputBox = $window.FindName("InputBox")
$status = $window.FindName("Status")
$window.FindName("Pass").Password = 'DO_NOT_EXPOSE_UIA_TEST_SECRET'
$window.FindName("ApplyButton").Add_Click({ $status.Text = 'Saved: ' + $inputBox.Text })
$timer = New-Object Windows.Threading.DispatcherTimer
$timer.Interval = [TimeSpan]::FromSeconds(90)
$timer.Add_Tick({ $window.Close() })
$timer.Start()
$null = $window.ShowDialog()
