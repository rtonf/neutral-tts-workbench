"""Windows worker-tree lifetime ownership; closing the job stops descendants."""
from __future__ import annotations

import os


class ProcessOwner:
    def __init__(self, process):
        self.process = process
        self.handle = None
        if os.name != "nt":
            return
        import ctypes as c
        from ctypes import wintypes as w
        class Basic(c.Structure):
            _fields_ = [("process_time", c.c_int64), ("job_time", c.c_int64), ("flags", w.DWORD),
                        ("minimum_working_set", c.c_size_t), ("maximum_working_set", c.c_size_t),
                        ("active_process_limit", w.DWORD), ("affinity", c.c_size_t),
                        ("priority", w.DWORD), ("scheduling", w.DWORD)]
        class IO(c.Structure):
            _fields_ = [(name, c.c_uint64) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
        class Extended(c.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_memory", c.c_size_t),
                        ("job_memory", c.c_size_t), ("peak_process", c.c_size_t), ("peak_job", c.c_size_t)]
        api = c.WinDLL("kernel32", use_last_error=True)
        api.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
        api.CreateJobObjectW.restype = w.HANDLE
        api.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        api.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        api.CloseHandle.argtypes = [w.HANDLE]
        self.api = api
        self.handle = api.CreateJobObjectW(None, None)
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.handle or not api.SetInformationJobObject(self.handle, 9, c.byref(limits), c.sizeof(limits)) or not api.AssignProcessToJobObject(self.handle, int(process._handle)):
            self.close()
            process.terminate()
            process.wait(timeout=5)
            raise OSError("Worker process-tree ownership is unavailable.")

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None

    def cancel(self):
        if os.name == "nt":
            self.close()
        elif self.process.poll() is None:
            self.process.terminate()
