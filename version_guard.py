"""키움 버전처리 대응과 Open API 단일 실행 보장. Qt·OCX에 의존하지 않음.

버전처리 팝업이 뜬 상태에서 OCX를 불러온 프로세스가 살아 있으면 khopenapi.ocx·opcommapi.dll·
opcomms.dll 교체가 거부되고(VerLog: Access denied, [183]) 다음 로그인마다 같은 팝업이 반복된다.
감시기는 그 신호를 보면 자기 프로세스를 끝내 파일 잠금을 풀고, 사용자는 팝업의 [확인]만 누른다.
"""
import datetime as dt
import os
import re
import sys
import threading
import time
from pathlib import Path

VERSION_EXIT = 77
BUSY_EXIT = 78
UPDATER = 'opstarter.exe'
# 로그인 창(opstarter)이 닫힌 뒤 약 12초 후 OPVersionUp.exe가 실제 파일을 교체한다(VerLog 22:05:02→22:05:14).
UPDATERS = (UPDATER, 'opversionup.exe')
KOA = 'koastudiosa.exe'
POPUP_TEXT = '버전처리'
CORE_FILES = ('khopenapi.ocx', 'opcommapi.dll', 'opcomms.dll')
CORE_DOWNLOAD = re.compile(r'(?:FileDown요구 |File)\[(' + '|'.join(re.escape(f) for f in CORE_FILES) + r')\]', re.I)
MUTEX_NAME = 'Local\\deriv_radar_openapi'


def is_version_popup(image, texts):
    return os.path.basename(str(image)).lower() in UPDATERS and any(POPUP_TEXT in str(t) for t in texts)


def core_update_in(text):
    """VerLog 신규 구간에서 이 프로세스가 잡고 있는 핵심 파일의 교체 다운로드를 찾는다."""
    m = CORE_DOWNLOAD.search(text or '')
    return m.group(1).lower() if m else None


def verlog_path(module_dir, day=None):
    day = day or dt.date.today()
    return Path(module_dir) / 'log' / ('VerLog.' + day.strftime('%Y%m%d'))


if sys.platform == 'win32':
    import ctypes
    from ctypes import wintypes
    _user32 = ctypes.WinDLL('user32', use_last_error=True)
    _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _ENUM = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _user32.EnumWindows.argtypes = [_ENUM, wintypes.LPARAM]
    _user32.EnumChildWindows.argtypes = [wintypes.HWND, _ENUM, wintypes.LPARAM]
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                            wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
    _user32.SendMessageTimeoutW.restype = ctypes.c_size_t
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel32.CreateMutexW.restype = wintypes.HANDLE

    class _ENTRY(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD), ('th32ProcessID', wintypes.DWORD),
                    ('th32DefaultHeapID', ctypes.c_size_t), ('th32ModuleID', wintypes.DWORD),
                    ('cntThreads', wintypes.DWORD), ('th32ParentProcessID', wintypes.DWORD),
                    ('pcPriClassBase', wintypes.LONG), ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260)]
    _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ENTRY)]
    _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ENTRY)]


def processes():
    """(pid, 실행파일 이름) 목록. Windows가 아니면 빈 목록."""
    if sys.platform != 'win32':
        return []
    snap = _kernel32.CreateToolhelp32Snapshot(2, 0)
    if not snap or snap == ctypes.c_void_p(-1).value:
        return []
    out = []
    try:
        entry = _ENTRY()
        entry.dwSize = ctypes.sizeof(_ENTRY)
        ok = _kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append((entry.th32ProcessID, entry.szExeFile))
            ok = _kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(snap)
    return out


def running(name):
    return [pid for pid, exe in processes() if exe.lower() == name]


def updater_running():
    return [pid for pid, exe in processes() if exe.lower() in UPDATERS]


def _text(hwnd):
    size = ctypes.c_size_t(0)
    if not _user32.SendMessageTimeoutW(hwnd, 0x000E, 0, 0, 0x0002, 300, ctypes.byref(size)):
        return ''
    buf = ctypes.create_unicode_buffer(size.value + 1)
    got = ctypes.c_size_t(0)
    _user32.SendMessageTimeoutW(hwnd, 0x000D, size.value + 1, ctypes.cast(buf, ctypes.c_void_p).value or 0,
                                0x0002, 300, ctypes.byref(got))
    return buf.value


def find_version_popup():
    """opstarter.exe가 띄운 '버전처리' 안내 창의 핸들. 없으면 None."""
    pids = set(updater_running())
    if not pids:
        return None
    found = []

    def top(hwnd, _):
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in pids or not _user32.IsWindowVisible(hwnd):
            return True
        texts = [_text(hwnd)]

        def child(h, _):
            texts.append(_text(h))
            return True
        _user32.EnumChildWindows(hwnd, _ENUM(child), 0)
        if is_version_popup(UPDATER, texts):
            found.append(hwnd)
            return False
        return True
    _user32.EnumWindows(_ENUM(top), 0)
    return found[0] if found else None


_mutex = None


def claim_single_instance():
    """이 폴더의 수집기·점검기 중 한 프로세스만 OCX를 쓰도록 이름 있는 뮤텍스를 잡는다."""
    global _mutex
    if sys.platform != 'win32' or _mutex:
        return True
    handle = _kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.get_last_error() == 183:
        _kernel32.CloseHandle(handle)
        return False
    _mutex = handle
    return True


def wait_updater(log=print, timeout=900, quiet=30):
    """키움 로그인·업데이트 프로그램(opstarter·OPVersionUp)이 quiet초 연속으로 없을 때까지 기다린다.

    opstarter가 닫힌 직후엔 OPVersionUp이 아직 뜨기 전이라, 짧게 확인하고 OCX를 다시 불러오면
    교체 중인 파일을 잠가 '파일 삭제 실패 [183]'이 난다.
    """
    if not updater_running():
        return True
    log('키움 로그인·업데이트 창이 열려 있습니다. 안내창의 [확인]을 눌러 업데이트를 마치세요. 업데이트 프로그램이 모두 끝나면 계속합니다.')
    end = time.monotonic() + timeout
    clear_since = None
    while time.monotonic() < end:
        time.sleep(1)
        if updater_running():
            clear_since = None
        elif clear_since is None:
            clear_since = time.monotonic()
        elif time.monotonic() - clear_since >= quiet:
            return True
    log('키움 업데이트가 %d분 안에 끝나지 않았습니다.' % (timeout // 60))
    return False


class VersionWatch:
    """로그인 동안 버전처리 신호(팝업 문구 또는 핵심 파일 교체 다운로드)를 감시한다."""

    def __init__(self, module_dir, on_detect, interval=.5):
        self.module_dir, self.on_detect, self.interval = Path(module_dir), on_detect, interval
        self.stop_event = threading.Event()
        self.log_path = verlog_path(self.module_dir)
        try:
            self.offset = self.log_path.stat().st_size
        except OSError:
            self.offset = 0
        self.thread = threading.Thread(target=self._run, name='version-watch', daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.stop_event.set()

    def check(self):
        try:
            with open(self.log_path, 'rb') as f:
                f.seek(self.offset)
                chunk = f.read()
            self.offset += len(chunk)
            name = core_update_in(chunk.decode('cp949', errors='ignore'))
            if name:
                return '키움 업데이트가 ' + name + ' 교체를 요청했습니다'
        except OSError:
            pass
        if sys.platform == 'win32' and find_version_popup():
            return '키움 버전처리 안내 창을 감지했습니다'
        return None

    def _run(self):
        while not self.stop_event.wait(self.interval):
            reason = self.check()
            if reason:
                self.on_detect(reason)
                return
