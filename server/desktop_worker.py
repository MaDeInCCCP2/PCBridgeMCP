"""One Windows desktop and one job, operated through UI Automation."""
import argparse
import os
import sys
import threading
import uuid

from uia_worker import UIError, UIService, WindowsBackend, serve


SYNCHRONIZE = 0x00100000
WAIT_FOREVER = 0xFFFFFFFF
APPLICATION_EXITED = 23


def watch_parent(pid):
    """An exited server also tears down a worker stuck in a UI provider."""
    import win32api
    import win32event

    handle = win32api.OpenProcess(SYNCHRONIZE, False, pid)

    def wait():
        try:
            win32event.WaitForSingleObject(handle, WAIT_FOREVER)
        finally:
            os._exit(0)

    threading.Thread(target=wait, name="server-lifetime", daemon=True).start()


class DesktopBackend(WindowsBackend):
    def __init__(self, desktop, job):
        import pywintypes
        import win32gui
        import win32job
        import win32process

        self.desktop_handle, self.job = desktop, job
        self.gui, self.jobs, self.processes = win32gui, win32job, win32process
        self.win32_error = pywintypes.error
        super().__init__()
        self.enum_callback = self.ctypes.WINFUNCTYPE(self.wintypes.BOOL, self.wintypes.HWND, self.wintypes.LPARAM)
        self.u32.EnumDesktopWindows.argtypes = [self.wintypes.HANDLE, self.enum_callback, self.wintypes.LPARAM]
        self.u32.EnumDesktopWindows.restype = self.wintypes.BOOL

    def foreground(self):
        windows = self.windows()
        if len(windows) != 1:
            raise UIError("ambiguous_window" if windows else "window_not_found",
                          "Select a job window with its hwnd:<number> from desktop_windows.", windows=windows[:30])
        return windows[0]["hwnd"]

    def _window_pids(self):
        processes = set(self.jobs.QueryInformationJobObject(self.job, self.jobs.JobObjectBasicProcessIdList))
        handles = []

        @self.enum_callback
        def record(hwnd, _):
            handles.append(hwnd)
            return True

        self.ctypes.set_last_error(0)
        if not self.u32.EnumDesktopWindows(int(self.desktop_handle), record, 0):
            code = self.ctypes.get_last_error()
            if code:
                raise UIError("desktop_enumeration_failed", "Could not enumerate this desktop's windows.", winerror=code)
        windows = {}
        for hwnd in handles:
            try:
                _, pid = self.processes.GetWindowThreadProcessId(hwnd)
                if pid in processes:
                    windows[hwnd] = pid
            except self.win32_error:
                continue
        return windows

    def windows(self):
        result = []
        for hwnd, pid in self._window_pids().items():
            try:
                if self.gui.IsWindowVisible(hwnd):
                    result.append({"hwnd": hwnd, "pid": pid, "title": self.gui.GetWindowText(hwnd)[:256],
                                   "exe": self.psutil.Process(pid).name()})
            except (self.win32_error, self.psutil.Error):
                continue
        return result

    def root(self, hwnd):
        pid = self._window_pids().get(hwnd)
        if pid is None:
            raise UIError("foreign_window", "Window is outside this desktop session.")
        root = super().root(hwnd)
        # COM lookup can race a window destruction or handle reuse.
        if int(root.element_info.process_id) != pid or self._window_pids().get(hwnd) != pid:
            raise UIError("foreign_window", "Window ownership changed before observation.")
        return root


class DesktopService:
    def __init__(self):
        self.original = self.desktop = self.job = self.ui = None
        self.stopping = threading.Event()
        self.monitor = None

    def monitor_job(self):
        import pywintypes
        import win32job

        while not self.stopping.wait(.25):
            try:
                accounting = win32job.QueryInformationJobObject(self.job, win32job.JobObjectBasicAccountingInformation)
            except pywintypes.error:
                return
            if accounting["ActiveProcesses"] == 0:
                # The worker can be blocked waiting for the next RPC. Process exit releases
                # the desktop, job and UIA handles without interrupting any remaining app process.
                os._exit(APPLICATION_EXITED)

    def start(self, executable, args, cwd):
        import pywintypes
        import win32api
        import win32con
        import win32job
        import win32service

        from desktop_process import create_desktop_process

        if self.desktop is not None:
            raise UIError("desktop_already_started", "This worker already owns a desktop.")
        name = "PCBridge_" + uuid.uuid4().hex
        access = (win32con.DESKTOP_CREATEWINDOW | win32con.DESKTOP_CREATEMENU
                  | win32con.DESKTOP_READOBJECTS | win32con.DESKTOP_WRITEOBJECTS | win32con.DESKTOP_ENUMERATE)
        try:
            self.original = win32service.GetThreadDesktop(win32api.GetCurrentThreadId())
            self.desktop = win32service.CreateDesktop(name, 0, access, None)
            self.desktop.SetThreadDesktop()
            self.job = win32job.CreateJobObject(None, "")
            limits = win32job.QueryInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation)
            limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation, limits)
            self.ui = UIService(DesktopBackend(self.desktop, self.job))
            pid = create_desktop_process(self.job, name, executable, args, cwd)
        except UIError:
            raise
        except (OSError, ValueError, pywintypes.error) as error:
            raise UIError("desktop_start_failed", "Windows could not start the application on a separate desktop.",
                          winerror=getattr(error, "winerror", None)) from error
        self.monitor = threading.Thread(target=self.monitor_job, name="desktop-job-lifetime", daemon=True)
        self.monitor.start()
        return {"pid": pid}

    def dispatch(self, operation, arguments):
        if operation == "start":
            return self.start(**arguments)
        if self.ui is None:
            raise UIError("desktop_not_started", "The desktop session is unavailable.")
        if operation == "windows":
            return {"windows": self.ui.backend.windows()[:50]}
        if operation == "wait":
            return self.ui.dispatch(operation, {**arguments, "wait_for_window": True})
        if operation in ("snapshot", "action"):
            return self.ui.dispatch(operation, arguments)
        raise UIError("unknown_operation", "Unknown desktop operation.")

    def close(self):
        self.stopping.set()
        if self.monitor is not None:
            self.monitor.join()
        if self.job is not None:
            self.job.Close()
        if self.desktop is not None:
            import pywintypes

            try:
                self.original.SetThreadDesktop()
                self.desktop.CloseDesktop()
            except pywintypes.error:
                # USER/COM can retain thread-owned windows until worker exit.
                pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", type=int, required=True)
    args = parser.parse_args()
    watch_parent(args.parent)
    service = DesktopService()
    replies = sys.stdout
    sys.stdout = sys.stderr
    try:
        serve(lambda: service, replies)
    finally:
        service.close()


if __name__ == "__main__":
    main()
