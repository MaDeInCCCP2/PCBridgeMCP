param([string]$Title)
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$form = New-Object System.Windows.Forms.Form
$form.Text = $Title
$form.Width = 480
$form.Height = 280
$form.StartPosition = 'CenterScreen'
$inputBox = New-Object System.Windows.Forms.TextBox
$inputBox.AccessibleName = 'PCBridge input'
$inputBox.Location = New-Object System.Drawing.Point(20,20)
$inputBox.Width = 300
$button = New-Object System.Windows.Forms.Button
$button.AccessibleName = 'PCBridge apply'
$button.Text = 'Apply'
$button.Location = New-Object System.Drawing.Point(20,60)
$status = New-Object System.Windows.Forms.Label
$status.Text = 'Ready'
$status.Location = New-Object System.Drawing.Point(20,100)
$status.Width = 400
$checkbox = New-Object System.Windows.Forms.CheckBox
$checkbox.AccessibleName = 'PCBridge flag'
$checkbox.Text = 'Flag'
$checkbox.Location = New-Object System.Drawing.Point(20,130)
$password = New-Object System.Windows.Forms.TextBox
$password.AccessibleName = 'PCBridge password'
$password.UseSystemPasswordChar = $true
$password.Text = 'DO_NOT_EXPOSE_UIA_TEST_SECRET'
$password.Location = New-Object System.Drawing.Point(20,165)
$button.Add_Click({ $status.Text = 'Saved: ' + $inputBox.Text })
$form.Controls.AddRange(@($inputBox, $button, $status, $checkbox, $password))
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 90000
$timer.Add_Tick({ $form.Close() })
$timer.Start()
[System.Windows.Forms.Application]::Run($form)
