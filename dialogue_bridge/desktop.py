"""Experimental project-bound transport for an already-running Codex desktop.

Only a locally configured Windows named pipe is used. No Codex authentication,
private databases, app-server processes, or browser-supplied project identities
are accessed. The desktop's local protocol may change between app versions.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import secrets
import struct
import time


MAX_FRAME_BYTES = 8 * 1024 * 1024
_IDENTIFIER = re.compile(r'[A-Za-z0-9_-]{1,128}')
_TOOLS = frozenset({'list_projects', 'create_thread', 'list_threads',
                    'read_thread', 'send_message_to_thread'})
_MUTATIONS = frozenset({'create_thread', 'send_message_to_thread'})


class DesktopError(Exception):
    def __init__(self, code, message, *, uncertain=False):
        super().__init__(message)
        self.code = code
        self.uncertain = uncertain


def _require(condition, code, message):
    if not condition:
        raise DesktopError(code, message)


def _id(value, label):
    _require(isinstance(value, str) and _IDENTIFIER.fullmatch(value),
             'desktop_binding', label + '无效。')
    return value


def _path_key(value):
    _require(isinstance(value, str) and Path(value).is_absolute(),
             'desktop_binding', 'Codex 项目目录必须是现有绝对路径。')
    return os.path.normcase(str(Path(value).resolve()))


def _encode_frame(payload):
    try:
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeError) as exc:
        raise DesktopError('desktop_protocol', 'Codex 请求格式无效。') from exc
    _require(0 < len(raw) <= MAX_FRAME_BYTES, 'desktop_protocol', 'Codex 请求超过大小限制。')
    return struct.pack('<I', len(raw)) + raw


def _decode_frame(frame):
    _require(isinstance(frame, (bytes, bytearray)) and len(frame) >= 4,
             'desktop_protocol', 'Codex 响应帧不完整。')
    length = struct.unpack('<I', frame[:4])[0]
    _require(0 < length <= MAX_FRAME_BYTES and len(frame) == length + 4,
             'desktop_protocol', 'Codex 响应帧长度无效。')
    try:
        value = json.loads(frame[4:].decode('utf-8'))
    except (ValueError, UnicodeError) as exc:
        raise DesktopError('desktop_protocol', 'Codex 响应不是有效 JSON。') from exc
    _require(isinstance(value, dict), 'desktop_protocol', 'Codex 响应必须是对象。')
    return value


def _native_request(pipe_path, method, params, *, timeout, mutating=False):
    """One bounded overlapped Windows pipe request; never replay a sent action."""
    _require(os.name == 'nt', 'desktop_unavailable', '当前平台不支持 Codex 桌面管道。')
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    invalid_handle = ctypes.c_void_p(-1).value

    class Overlapped(ctypes.Structure):
        _fields_ = [('Internal', ctypes.c_size_t), ('InternalHigh', ctypes.c_size_t),
                    ('Offset', wintypes.DWORD), ('OffsetHigh', wintypes.DWORD),
                    ('hEvent', wintypes.HANDLE)]

    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    kernel.WaitNamedPipeW.restype = wintypes.BOOL
    kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateEventW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.POINTER(Overlapped),
                                         ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
    kernel.GetOverlappedResult.restype = wintypes.BOOL
    kernel.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(Overlapped)]
    kernel.CancelIoEx.restype = wintypes.BOOL
    for name in ('ReadFile', 'WriteFile'):
        function = getattr(kernel, name)
        function.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                             ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(Overlapped)]
        function.restype = wintypes.BOOL

    deadline = time.monotonic() + timeout
    request_id = secrets.token_hex(12)
    request = _encode_frame({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})
    handle = None
    attempted = False

    def remaining():
        duration = deadline - time.monotonic()
        if duration <= 0:
            raise DesktopError('desktop_timeout', '等待当前 Codex 客户端超时。',
                               uncertain=mutating and attempted)
        return max(1, min(int(duration * 1000), 0xfffffffe))

    def transfer(buffer, count, writing):
        nonlocal attempted
        event = kernel.CreateEventW(None, True, False, None)
        if not event:
            raise DesktopError('desktop_unavailable', '无法创建 Codex 桌面连接事件。',
                               uncertain=mutating and attempted)
        operation = Overlapped(hEvent=event)
        amount = wintypes.DWORD()
        try:
            remaining()
            if writing:
                attempted = True
            function = kernel.WriteFile if writing else kernel.ReadFile
            complete = function(handle, buffer, count, ctypes.byref(amount), ctypes.byref(operation))
            if not complete:
                error = ctypes.get_last_error()
                if error != 997:  # ERROR_IO_PENDING
                    raise DesktopError('desktop_unavailable', '当前 Codex 桌面连接已中断。',
                                       uncertain=mutating and attempted)
                try:
                    result = kernel.WaitForSingleObject(event, remaining())
                    if result != 0:  # WAIT_OBJECT_0
                        raise DesktopError('desktop_timeout', '等待当前 Codex 客户端超时。',
                                           uncertain=mutating and attempted)
                except Exception:
                    kernel.CancelIoEx(handle, ctypes.byref(operation))
                    # Keep the OVERLAPPED and its buffer alive until cancellation completes.
                    kernel.GetOverlappedResult(handle, ctypes.byref(operation), ctypes.byref(amount), True)
                    raise
                if not kernel.GetOverlappedResult(handle, ctypes.byref(operation), ctypes.byref(amount), False):
                    raise DesktopError('desktop_unavailable', '当前 Codex 桌面连接已中断。',
                                       uncertain=mutating and attempted)
            if not amount.value:
                raise DesktopError('desktop_unavailable', '当前 Codex 桌面连接已关闭。',
                                   uncertain=mutating and attempted)
            return amount.value
        finally:
            kernel.CloseHandle(event)

    def receive(length):
        pieces = []
        while length:
            size = min(length, 128 * 1024)
            buffer = ctypes.create_string_buffer(size)
            count = transfer(buffer, size, False)
            pieces.append(buffer.raw[:count])
            length -= count
        return b''.join(pieces)

    def receive_frame():
        header = receive(4)
        length = struct.unpack('<I', header)[0]
        _require(0 < length <= MAX_FRAME_BYTES, 'desktop_protocol', 'Codex 响应超过大小限制。')
        return _decode_frame(header + receive(length))

    try:
        while True:
            remaining()
            handle = kernel.CreateFileW(pipe_path, 0xc0000000, 0, None, 3, 0x40000000, None)
            if handle != invalid_handle:
                break
            handle = None
            if ctypes.get_last_error() != 231:  # ERROR_PIPE_BUSY
                raise DesktopError('desktop_unavailable', '无法连接当前 Codex 客户端，请保持客户端开启。')
            if not kernel.WaitNamedPipeW(pipe_path, remaining()):
                raise DesktopError('desktop_unavailable', '当前 Codex 客户端暂时无法连接。')
        offset = 0
        while offset < len(request):
            piece = request[offset:]
            buffer = ctypes.create_string_buffer(piece)
            offset += transfer(buffer, len(piece), True)
        response = receive_frame()
        _require(response.get('jsonrpc') == '2.0' and response.get('id') == request_id,
                 'desktop_protocol', 'Codex 响应身份与请求不一致。')
        return response
    except DesktopError as exc:
        if mutating and attempted and exc.code == 'desktop_protocol':
            exc.uncertain = True
        raise
    except OSError as exc:
        raise DesktopError('desktop_unavailable', '当前 Codex 桌面连接失败。',
                           uncertain=mutating and attempted) from exc
    finally:
        if handle not in (None, invalid_handle):
            kernel.CloseHandle(handle)


class DesktopClient:
    """Use only one configured local project in the running desktop application."""

    def __init__(self, binding_file, project_dir, *, timeout=35, requester=None):
        self.binding_file = Path(binding_file).absolute()
        self.project_dir = Path(project_dir).resolve()
        _require(self.project_dir.is_dir(), 'desktop_binding', '绑定项目目录不存在。')
        _require(isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
                 and math.isfinite(timeout) and 0 < timeout <= 300,
                 'desktop_binding', '桌面请求超时须在 0 至 300 秒之间。')
        self.timeout = float(timeout)
        self.requester = requester or _native_request

    def _binding(self):
        path = self.binding_file
        _require(path.is_file() and not path.is_symlink(), 'desktop_binding',
                 '请在本机配置有效的桌面项目绑定文件。')
        try:
            _require(path.stat().st_size <= 65536, 'desktop_binding', '桌面绑定文件过大。')
            binding = json.loads(path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError, UnicodeError) as exc:
            raise DesktopError('desktop_binding', '桌面项目绑定无法读取。') from exc
        _require(isinstance(binding, dict) and type(binding.get('schema_version')) is int
                 and binding['schema_version'] == 1, 'desktop_binding', '桌面绑定版本无效。')
        for key in ('project_id', 'caller_thread_id'):
            _id(binding.get(key), key)
        _require(binding.get('host_id') == 'local', 'desktop_binding', '只支持本地项目。')
        _require(_path_key(binding.get('project_path')) == _path_key(str(self.project_dir)),
                 'desktop_binding', '桌面绑定必须与服务端配置的项目目录一致。')
        pipe = binding.get('pipe_path')
        _require(isinstance(pipe, str) and pipe.startswith('\\\\.\\pipe\\')
                 and 9 < len(pipe) <= 1024 and not any(ord(c) < 32 for c in pipe),
                 'desktop_binding', '桌面管道绑定无效。')
        return {key: binding[key] for key in ('schema_version', 'project_id', 'project_path',
                                             'host_id', 'caller_thread_id', 'pipe_path')}

    def _bound(self, expected=None):
        binding = self._binding()
        if expected is not None:
            _require(isinstance(expected, dict) and all(expected.get(k) == binding[k]
                     for k in ('project_id', 'host_id'))
                     and _path_key(expected.get('project_path')) == _path_key(binding['project_path']),
                     'desktop_project', '原任务所属项目与当前绑定不一致。')
        return binding

    def _call(self, tool, arguments, binding):
        _require(tool in _TOOLS and isinstance(arguments, dict), 'desktop_input', '不支持的桌面操作。')
        # A configuration change during one delivery must not move its mutation.
        current = self._binding()
        _require(current == binding, 'desktop_binding', '桌面连接绑定在投递期间发生变化。')
        mutating = tool in _MUTATIONS
        params = {'arguments': arguments, 'callerSource': 'codex',
                  'callId': 'dialogue-bridge-' + secrets.token_hex(16), 'namespace': 'codex_app',
                  'threadId': binding['caller_thread_id'], 'tool': tool,
                  'turnId': 'bridge-turn-' + secrets.token_hex(12)}
        try:
            response = self.requester(binding['pipe_path'], 'tools/call', params,
                                      timeout=self.timeout, mutating=mutating)
        except DesktopError:
            raise
        except (TimeoutError, OSError) as exc:
            raise DesktopError('desktop_transport', '桌面请求未能确认结果。',
                               uncertain=mutating) from exc
        try:
            _require(isinstance(response, dict), 'desktop_protocol', '桌面响应格式无效。')
            _require(not response.get('error'), 'desktop_request', '桌面客户端未确认该操作。')
            result = response.get('result')
            _require(isinstance(result, dict) and result.get('success') is True
                     and isinstance(result.get('contentItems'), list),
                     'desktop_protocol', '桌面响应缺少成功回执。')
            texts = [item.get('text') for item in result['contentItems']
                     if isinstance(item, dict) and item.get('type') == 'inputText']
            _require(len(texts) == 1 and isinstance(texts[0], str),
                     'desktop_protocol', '桌面结果不明确。')
            try:
                value = json.loads(texts[0])
            except (ValueError, UnicodeError) as exc:
                raise DesktopError('desktop_protocol', '桌面结果不是有效 JSON。') from exc
            _require(isinstance(value, dict), 'desktop_protocol', '桌面结果必须是对象。')
            return value
        except DesktopError as exc:
            # A sent native mutation has no server-side idempotency key. An error
            # alone cannot prove that it did not create a chat or enqueue a turn.
            if mutating:
                exc.uncertain = True
            raise

    def _project(self, expected=None):
        bound = self._bound(expected)
        projects = self._call('list_projects', {}, bound).get('projects')
        _require(isinstance(projects, list), 'desktop_project', '无法核对桌面项目列表。')
        matches = [p for p in projects if isinstance(p, dict)
                   and p.get('projectId') == bound['project_id']]
        _require(len(matches) == 1 and matches[0].get('projectKind') == 'local'
                 and matches[0].get('hostId') == bound['host_id']
                 and _path_key(matches[0].get('path')) == _path_key(bound['project_path']),
                 'desktop_project', '绑定项目不在当前桌面客户端中，或项目身份已变化。')
        return bound

    def check(self):
        """Read-only availability check; it neither creates nor messages a chat."""
        binding = self._project()
        return {'adapter': 'codex-desktop', 'available': True,
                'host_id': binding['host_id'], 'project_id': binding['project_id'],
                'project_path': binding['project_path']}

    def create(self, prompt, title):
        _require(isinstance(prompt, str) and prompt.strip() and isinstance(title, str) and title.strip(),
                 'desktop_input', '任务名称和提示不能为空。')
        binding = self._project()
        result = self._call('create_thread', {'prompt': prompt, 'title': title,
            'target': {'type': 'project', 'projectId': binding['project_id'],
                       'environment': {'type': 'local'}}}, binding)
        try:
            _require(result.get('hostId', binding['host_id']) == binding['host_id'],
                     'desktop_result', '新对话主机与绑定不符。')
            if 'projectId' in result:
                _require(result['projectId'] == binding['project_id'], 'desktop_result', '新对话项目与绑定不符。')
            if 'projectPath' in result:
                _require(_path_key(result['projectPath']) == _path_key(binding['project_path']),
                         'desktop_result', '新对话目录与绑定不符。')
            # A queued clientThreadId is not the durable identity required for
            # replies. Keep an uncertain outbox event instead of creating twice.
            thread_id = _id(result.get('threadId'), '新对话 ID')
            return {'adapter': 'codex-desktop', 'thread_id': thread_id,
                    'host_id': binding['host_id'], 'project_id': binding['project_id'],
                    'project_path': binding['project_path']}
        except DesktopError as exc:
            exc.uncertain = True
            raise

    def _verify_thread(self, thread_id, binding):
        raw = self._call('read_thread', {'threadId': thread_id, 'hostId': binding['host_id'],
            'turnLimit': 1, 'includeOutputs': False, 'maxOutputCharsPerItem': 0}, binding)
        thread = raw.get('thread')
        _require(isinstance(thread, dict) and (thread.get('id') or thread.get('threadId')) == thread_id
                 and thread.get('hostId') == binding['host_id'],
                 'desktop_thread', '桌面返回了不同的对话身份。')
        if 'projectId' in thread:
            _require(thread['projectId'] == binding['project_id'],
                     'desktop_thread', '原对话不属于配置的项目。')
        else:
            listing = self._call('list_threads', {'limit': 50}, binding)
            pinned, recent = listing.get('pinnedThreads', []), listing.get('threads', [])
            _require(isinstance(pinned, list) and isinstance(recent, list),
                     'desktop_protocol', '桌面对话列表格式无效。')
            matches = [t for t in pinned + recent if isinstance(t, dict)
                       and (t.get('id') or t.get('threadId')) == thread_id
                       and t.get('hostId') == binding['host_id']]
            _require(matches and all(t.get('projectId') == binding['project_id'] for t in matches),
                     'desktop_thread', '无法在配置的项目中核对原对话。')
        if thread.get('cwd'):
            _require(_path_key(thread['cwd']) == _path_key(binding['project_path']),
                     'desktop_thread', '原对话的执行目录与配置项目不同。')
        return thread

    def send(self, thread_id, prompt, expected):
        _id(thread_id, '原对话 ID')
        _require(isinstance(prompt, str) and prompt.strip(), 'desktop_input', '续接内容不能为空。')
        binding = self._project(expected)
        self._verify_thread(thread_id, binding)
        result = self._call('send_message_to_thread', {'threadId': thread_id,
                            'hostId': binding['host_id'], 'prompt': prompt}, binding)
        try:
            if 'threadId' in result:
                _require(result['threadId'] == thread_id, 'desktop_result', '消息回执属于另一条对话。')
            if 'hostId' in result:
                _require(result['hostId'] == binding['host_id'], 'desktop_result', '消息回执主机不同。')
        except DesktopError as exc:
            exc.uncertain = True
            raise
        return {'adapter': 'codex-desktop', 'thread_id': thread_id,
                'host_id': binding['host_id'], 'project_id': binding['project_id'],
                'project_path': binding['project_path']}
