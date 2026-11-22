using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Threading;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Automation;
using Microsoft.UI.Xaml.Controls;

internal sealed class Fixture : Application
{
    private Window? window;

    [StructLayout(LayoutKind.Sequential)]
    private struct Rect
    {
        public int Left, Top, Right, Bottom;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct MonitorInfo
    {
        public uint Size;
        public Rect Monitor, Work;
        public uint Flags;
    }

    private delegate bool MonitorCallback(IntPtr monitor, IntPtr dc, IntPtr bounds, IntPtr data);

    [DllImport("user32.dll")]
    private static extern bool EnumDisplayMonitors(IntPtr dc, IntPtr clip, MonitorCallback callback, IntPtr data);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern bool GetMonitorInfo(IntPtr monitor, ref MonitorInfo info);

    [DllImport("user32.dll")]
    private static extern IntPtr MonitorFromWindow(IntPtr window, uint flags);

    [DllImport("user32.dll")]
    private static extern bool SetWindowPos(IntPtr window, IntPtr after, int x, int y, int width, int height, uint flags);

    [DllImport("user32.dll")]
    private static extern uint GetDpiForWindow(IntPtr windowHandle);

    [STAThread]
    public static void Main()
    {
        WinRT.ComWrappersSupport.InitializeComWrappers();
        Start(_ =>
        {
            SynchronizationContext.SetSynchronizationContext(
                new DispatcherQueueSynchronizationContext(DispatcherQueue.GetForCurrentThread()));
            new Fixture();
        });
    }

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        int clicks = 0;
        var status = new TextBlock { Text = "PCBridge clicks: 0" };
        AutomationProperties.SetName(status, status.Text);

        var button = new Button { Content = "PCBridge apply" };
        button.Click += (_, _) =>
        {
            status.Text = $"PCBridge clicks: {++clicks}";
            AutomationProperties.SetName(status, status.Text);
        };

        var panel = new StackPanel { Padding = new Thickness(24), Spacing = 12 };
        panel.Children.Add(button);
        panel.Children.Add(status);
        window = new Window { Title = "PCBridge WinUI Fixture", Content = panel };
        window.Activate();
        if (Array.IndexOf(Environment.GetCommandLineArgs(), "--monitor-test") >= 0)
            AddMonitorTest(window, panel);
    }

    private static void AddMonitorTest(Window window, StackPanel panel)
    {
        var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(window);
        const uint nearestMonitor = 2;
        const uint noActivateOrZOrder = 0x0010 | 0x0004;
        var first = MonitorFromWindow(hwnd, nearestMonitor);
        var monitors = new List<IntPtr>();
        EnumDisplayMonitors(IntPtr.Zero, IntPtr.Zero, (monitor, _, _, _) =>
        {
            if (monitor != first)
                monitors.Add(monitor);
            return true;
        }, IntPtr.Zero);
        var second = monitors.Count == 0 ? IntPtr.Zero : monitors[0];
        var move = new Button { Content = "PCBridge move to another monitor", IsEnabled = second != IntPtr.Zero };
        var result = new TextBlock { Text = second == IntPtr.Zero ? "PCBridge second monitor unavailable" : "PCBridge monitor ready" };
        panel.Children.Add(move);
        panel.Children.Add(result);
        if (second == IntPtr.Zero)
            return;

        move.Click += (_, _) =>
        {
            var before = MonitorFromWindow(hwnd, nearestMonitor);
            uint dpiBefore = GetDpiForWindow(hwnd);
            var target = new MonitorInfo { Size = (uint)Marshal.SizeOf<MonitorInfo>() };
            bool positioned = GetMonitorInfo(second, ref target)
                && SetWindowPos(hwnd, IntPtr.Zero, target.Work.Left + 24, target.Work.Top + 48, 400, 300, noActivateOrZOrder);
            var after = MonitorFromWindow(hwnd, nearestMonitor);
            uint dpiAfter = GetDpiForWindow(hwnd);
            bool moved = positioned && before == first && after == second && dpiBefore > 0 && dpiAfter > 0;
            result.Text = moved
                ? $"PCBridge monitor moved: {dpiBefore} to {dpiAfter}"
                : "PCBridge monitor move failed";
            AutomationProperties.SetName(result, result.Text);
        };
    }
}
