"""Launch into a Win32 desktop and job atomically on Windows 10 or later."""
import ctypes
import msvcrt
import os
import subprocess
from ctypes import wintypes


SIZE_T = ctypes.c_size_t
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D
STARTF_FORCEOFFFEEDBACK = 0x00000080
STARTF_USESTDHANDLES = 0x00000100
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_NO_WINDOW = 0x08000000


class StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD), ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD), ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE),
    ]


class StartupInfoEx(ctypes.Structure):
    _fields_ = [("info", StartupInfo), ("attributes", ctypes.c_void_p)]


class ProcessInformation(ctypes.Structure):
    _fields_ = [("process", wintypes.HANDLE), ("thread", wintypes.HANDLE),
                ("pid", wintypes.DWORD), ("tid", wintypes.DWORD)]


def create_desktop_process(job, desktop, executable, args, cwd):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(SIZE_T)]
    kernel.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    kernel.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, SIZE_T, ctypes.c_void_p, SIZE_T,
        ctypes.c_void_p, ctypes.c_void_p]
    kernel.UpdateProcThreadAttribute.restype = wintypes.BOOL
    kernel.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    kernel.DeleteProcThreadAttributeList.restype = None
    kernel.CreateProcessW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL,
        wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(StartupInfoEx),
        ctypes.POINTER(ProcessInformation)]
    kernel.CreateProcessW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    size = SIZE_T()
    kernel.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
    if not size.value:
        raise ctypes.WinError(ctypes.get_last_error())
    attributes = ctypes.create_string_buffer(size.value)
    if not kernel.InitializeProcThreadAttributeList(attributes, 2, 0, ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        with open(os.devnull, "r+b", buffering=0) as null:
            handle = msvcrt.get_osfhandle(null.fileno())
            os.set_handle_inheritable(handle, True)
            # Keep the arrays alive through CreateProcessW and attribute-list destruction.
            jobs = (wintypes.HANDLE * 1)(int(job))
            streams = (wintypes.HANDLE * 1)(handle)
            for attribute, values in ((PROC_THREAD_ATTRIBUTE_JOB_LIST, jobs),
                                      (PROC_THREAD_ATTRIBUTE_HANDLE_LIST, streams)):
                if not kernel.UpdateProcThreadAttribute(attributes, 0, attribute, values,
                                                        ctypes.sizeof(values), None, None):
                    raise ctypes.WinError(ctypes.get_last_error())

            startup = StartupInfoEx()
            startup.info.cb = ctypes.sizeof(startup)
            startup.info.lpDesktop = desktop
            startup.info.dwFlags = STARTF_USESTDHANDLES | STARTF_FORCEOFFFEEDBACK
            startup.info.hStdInput = startup.info.hStdOutput = startup.info.hStdError = handle
            startup.attributes = ctypes.addressof(attributes)
            command = ctypes.create_unicode_buffer(subprocess.list2cmdline([executable, *args]))
            process = ProcessInformation()
            if not kernel.CreateProcessW(executable, command, None, None, True,
                                         EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW, None, cwd,
                                         ctypes.byref(startup), ctypes.byref(process)):
                raise ctypes.WinError(ctypes.get_last_error())
            kernel.CloseHandle(process.thread)
            kernel.CloseHandle(process.process)
            return process.pid
    finally:
        kernel.DeleteProcThreadAttributeList(attributes)
