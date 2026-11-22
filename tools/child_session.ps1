param([switch]$CheckControl, [switch]$Connect)

$ErrorActionPreference = 'Stop'
if ($CheckControl -eq $Connect) { throw 'Choose -CheckControl or -Connect.' }
if ([System.Threading.Thread]::CurrentThread.ApartmentState -ne 'STA') {
    throw 'Run this script with Windows PowerShell -STA.'
}

Add-Type -ReferencedAssemblies 'System','System.Core','System.Windows.Forms','System.Drawing','Microsoft.CSharp' -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace PCBridge.ChildSession
{
    [ComImport]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    [Guid("302D8188-0052-4807-806A-362B628F9AC5")]
    interface IRdpExtendedSettings
    {
        void PutProperty([MarshalAs(UnmanagedType.BStr)] string name,
                         [MarshalAs(UnmanagedType.Struct)] ref object value);
        void GetProperty([MarshalAs(UnmanagedType.BStr)] string name,
                         [MarshalAs(UnmanagedType.Struct)] out object value);
    }

    sealed class RdpControl : AxHost
    {
        public RdpControl() : base("A0C63C30-F08D-4AB4-907C-34905D770C7D") {}

        public void Configure()
        {
            dynamic client = GetOcx();
            var extra = (IRdpExtendedSettings)GetOcx();
            object child = true;
            extra.PutProperty("ConnectToChildSession", ref child);
            extra.GetProperty("ConnectToChildSession", out child);
            if (!object.Equals(child, true))
                throw new InvalidOperationException("RDP did not select the local child session.");

            client.Server = "localhost";
            dynamic settings = client.AdvancedSettings7;
            settings.EnableCredSspSupport = true;
            settings.RedirectClipboard = false;
            settings.RedirectDrives = false;
            settings.RedirectPrinters = false;
            settings.RedirectSmartCards = false;
        }

        public void ConnectChild()
        {
            dynamic client = GetOcx();
            client.Connect();
        }
    }

    public static class Viewer
    {
        [DllImport("wtsapi32.dll", SetLastError = true)]
        static extern bool WTSIsChildSessionsEnabled(out bool enabled);

        public static bool Enabled()
        {
            bool enabled;
            if (!WTSIsChildSessionsEnabled(out enabled))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            return enabled;
        }

        public static void Run(bool connect)
        {
            if (connect && !Enabled())
                throw new InvalidOperationException("Windows Child Sessions are disabled.");

            using (var window = new Form())
            using (var control = new RdpControl())
            {
                window.Text = "PCBridge Windows Child Session";
                window.Width = 975;
                window.Height = 695;
                control.Dock = DockStyle.Fill;
                ((ISupportInitialize)control).BeginInit();
                window.Controls.Add(control);
                ((ISupportInitialize)control).EndInit();
                // Creating handles alone keeps the check mode invisible.
                IntPtr windowHandle = window.Handle;
                IntPtr controlHandle = control.Handle;
                control.Configure();
                if (connect)
                {
                    window.Shown += delegate
                    {
                        try { control.ConnectChild(); }
                        catch (Exception error)
                        {
                            MessageBox.Show(window, "Windows could not open the child session: " +
                                error.GetType().Name, window.Text);
                            window.Close();
                        }
                    };
                    Application.Run(window);
                }
            }
        }
    }
}
'@

if ($CheckControl) {
    [PCBridge.ChildSession.Viewer]::Run($false)
    [pscustomobject]@{ control_ready = $true; child_sessions_enabled = [PCBridge.ChildSession.Viewer]::Enabled() } | ConvertTo-Json -Compress
} else {
    [PCBridge.ChildSession.Viewer]::Run($true)
}
