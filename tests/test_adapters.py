from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.client import IncompleteRead
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import URLError

from dialogue_bridge.adapters import (DemoAdapter, DeliveryError, DesktopAdapter,
                                      WebhookAdapter, make_adapter)
from dialogue_bridge.desktop import DesktopError


def sample_task():
    return {'id': 'task_example', 'title': 'Example upload', 'data': {'amount': 3},
            'files': [], 'thread': None}


def sample_event(kind='dialogue.start'):
    return {'id': 'event_example', 'task_id': 'task_example', 'type': kind}


class CallbackServer:
    def __init__(self):
        self.requests = []
        self.status = 200
        self.reply = {'accepted': True, 'thread_id': 'thread_example'}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                owner.requests.append((self.path, dict(self.headers), json.loads(body)))
                raw = owner.reply if isinstance(owner.reply, bytes) else json.dumps(owner.reply).encode()
                self.send_response(owner.status)
                self.send_header('Content-Type', 'application/json')
                if owner.status in (301, 302, 307, 308):
                    self.send_header('Location', '/must-not-follow')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port) + '/callback'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class AdapterTests(unittest.TestCase):
    def server(self):
        receiver = CallbackServer()
        self.addCleanup(receiver.close)
        return receiver

    def test_demo_is_explicitly_not_a_real_ai_and_reuses_identity(self):
        task = sample_task()
        adapter = DemoAdapter()
        receipt = adapter.deliver(sample_event(), task)
        self.assertFalse(receipt['real_ai'])
        task['thread'] = receipt
        again = adapter.deliver(sample_event('dialogue.reply'), task)
        self.assertEqual(again['thread_id'], receipt['thread_id'])
        with self.assertRaises(DeliveryError):
            adapter.deliver(sample_event(), task)

    def test_webhook_posts_idempotent_event_and_credential_only_in_header(self):
        receiver = self.server()
        adapter = WebhookAdapter(receiver.url, 'test-only-secret')
        receipt = adapter.deliver(sample_event(), sample_task())
        self.assertEqual(receipt, {'adapter': 'webhook', 'thread_id': 'thread_example',
                                  'callback_peer': adapter.callback_peer})
        path, headers, body = receiver.requests[0]
        self.assertEqual(path, '/callback')
        self.assertEqual(headers['Authorization'], 'Bearer test-only-secret')
        self.assertEqual(headers['Idempotency-Key'], 'event_example')
        self.assertEqual(body['type'], 'dialogue.start')
        self.assertEqual(body['task']['data'], {'amount': 3})
        self.assertNotIn('test-only-secret', json.dumps(body))
        self.assertNotIn('test-only-secret', json.dumps(receipt))

    def test_webhook_reply_cannot_replace_original_thread(self):
        receiver = self.server()
        adapter = WebhookAdapter(receiver.url)
        task = sample_task()
        task['thread'] = {'adapter': 'webhook', 'thread_id': 'the_original_thread',
                          'callback_peer': adapter.callback_peer}
        with self.assertRaises(DeliveryError) as error:
            adapter.deliver(sample_event('dialogue.reply'), task)
        self.assertTrue(error.exception.uncertain)
        self.assertEqual(len(receiver.requests), 1)

    def test_webhook_omits_local_file_paths_and_retains_authenticated_download_routes(self):
        receiver = self.server()
        task = sample_task()
        with tempfile.TemporaryDirectory() as temp:
            local = str(Path(temp) / 'private-upload')
            task['files'] = [{'name': 'sample.txt', 'storage_name': 'file_example',
                              'local_path': local,
                              'download_path': '/api/worker/tasks/task_example/files/file_example'}]
            WebhookAdapter(receiver.url).deliver(sample_event(), task)
            body = receiver.requests[0][2]
            sent_file = body['task']['files'][0]
            self.assertNotIn('local_path', sent_file)
            self.assertNotIn(local, json.dumps(body))
            self.assertEqual(sent_file['download_path'], task['files'][0]['download_path'])
            self.assertEqual(task['files'][0]['local_path'], local)

    def test_switching_adapter_rejects_original_thread_before_any_callback(self):
        receiver = self.server()
        task = sample_task()
        task['thread'] = DemoAdapter().deliver(sample_event(), task)
        with self.assertRaises(DeliveryError) as error:
            WebhookAdapter(receiver.url).deliver(sample_event('dialogue.reply'), task)
        self.assertFalse(error.exception.uncertain)
        self.assertEqual(receiver.requests, [])
        task['thread'] = {'adapter': 'webhook', 'thread_id': 'thread_example'}
        with self.assertRaises(DeliveryError):
            DemoAdapter().deliver(sample_event('dialogue.reply'), task)

    def test_switching_callback_peer_rejects_original_thread(self):
        receiver = self.server()
        original = WebhookAdapter(receiver.url)
        task = sample_task()
        task['thread'] = original.deliver(sample_event(), task)
        receiver.requests.clear()
        replacement = WebhookAdapter(receiver.url + '/different-receiver')
        with self.assertRaises(DeliveryError) as error:
            replacement.deliver(sample_event('dialogue.reply'), task)
        self.assertFalse(error.exception.uncertain)
        self.assertEqual(receiver.requests, [])

    def test_webhook_missing_original_backend_proof_is_rejected(self):
        receiver = self.server()
        task = sample_task()
        task['thread'] = {'thread_id': 'thread_example'}
        with self.assertRaises(DeliveryError):
            WebhookAdapter(receiver.url).deliver(sample_event('dialogue.reply'), task)
        self.assertEqual(receiver.requests, [])

    def test_webhook_explicit_not_accepted_can_be_retried(self):
        receiver = self.server()
        receiver.status = 503
        receiver.reply = {'accepted': False, 'retryable': True}
        with self.assertRaises(DeliveryError) as error:
            WebhookAdapter(receiver.url).deliver(sample_event(), sample_task())
        self.assertFalse(error.exception.uncertain)

    def test_webhook_explicit_negative_acknowledgement_on_success_status(self):
        receiver = self.server()
        receiver.reply = {'accepted': False, 'retryable': True}
        with self.assertRaises(DeliveryError) as error:
            WebhookAdapter(receiver.url).deliver(sample_event(), sample_task())
        self.assertFalse(error.exception.uncertain)

    def test_webhook_connection_failure_and_incomplete_response_certainty(self):
        class Opener:
            def __init__(self, exception):
                self.exception = exception

            def open(self, *_args, **_kwargs):
                raise self.exception

        cases = [(URLError(ConnectionRefusedError()), False),
                 (URLError(TimeoutError()), True), (IncompleteRead(b'', 10), True)]
        for exception, uncertain in cases:
            with self.subTest(exception=type(exception).__name__), self.assertRaises(DeliveryError) as error:
                WebhookAdapter('http://example.test/', opener=Opener(exception)).deliver(
                    sample_event(), sample_task())
            self.assertIs(error.exception.uncertain, uncertain)

    def test_webhook_generic_error_is_uncertain_and_redacted(self):
        receiver = self.server()
        receiver.status = 500
        receiver.reply = {'error': 'private-service-details test-only-secret'}
        with self.assertRaises(DeliveryError) as error:
            WebhookAdapter(receiver.url, 'test-only-secret').deliver(sample_event(), sample_task())
        self.assertTrue(error.exception.uncertain)
        self.assertNotIn('test-only-secret', str(error.exception))
        self.assertNotIn(receiver.url, str(error.exception))

    def test_webhook_does_not_follow_redirect(self):
        receiver = self.server()
        receiver.status = 307
        receiver.reply = {}
        with self.assertRaises(DeliveryError) as error:
            WebhookAdapter(receiver.url, 'test-only-secret').deliver(sample_event(), sample_task())
        self.assertTrue(error.exception.uncertain)
        self.assertEqual(len(receiver.requests), 1)
        self.assertEqual(receiver.requests[0][0], '/callback')

    def test_webhook_malformed_success_does_not_mean_delivered(self):
        receiver = self.server()
        for value in (b'not-json', {}, {'accepted': True, 'thread_id': 'bad/id'},
                      {'accepted': True, 'thread_id': 10}, b'x' * 65537):
            receiver.reply = value
            with self.subTest(value=str(value)[:50]), self.assertRaises(DeliveryError) as error:
                WebhookAdapter(receiver.url).deliver(sample_event(), sample_task())
            self.assertTrue(error.exception.uncertain)

    def test_bad_url_and_header_fail_before_send(self):
        for url in ('file:///etc/hosts', 'http://user:password@example.test/',
                    'http://example.test/#fragment', 'http://example.test:99999/',
                    'http://example.test/\nextra'):
            with self.subTest(url=url), self.assertRaises(DeliveryError):
                WebhookAdapter(url)
        with self.assertRaises(DeliveryError):
            WebhookAdapter('http://example.test/', 'unsafe\nheader')

    def test_desktop_adapter_builds_local_cli_flow_without_exposing_binding(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            calls = []

            class Client:
                def create(self, prompt, title):
                    calls.append(('create', prompt, title))
                    return {'adapter': 'codex-desktop', 'thread_id': 'the_original_thread',
                            'host_id': 'local', 'project_id': 'project_example',
                            'project_path': str(root)}

                def send(self, thread_id, prompt, expected):
                    calls.append(('send', prompt, thread_id))
                    return expected

                def check(self):
                    return {'available': True}

            adapter = DesktopAdapter({'desktop_binding_file': str(root/'binding.json')},
                data_dir=root/'state', project_dir=root, cli_path=root/'bridge.py', client=Client())
            task = sample_task()
            task['data'] = {'huge_private_value': 'not-embedded'}
            receipt = adapter.deliver(sample_event(), task)
            prompt = calls[0][1]
            self.assertIn(str(root/'bridge.py'), prompt)
            self.assertIn('--data-dir', prompt)
            self.assertIn('publish-questions', prompt)
            self.assertIn('complete', prompt)
            self.assertIn('原对话', prompt)
            self.assertNotIn('huge_private_value', prompt)
            self.assertNotIn('binding.json', prompt)
            task['thread'] = receipt
            reply = adapter.deliver(sample_event('dialogue.reply'), task)
            self.assertEqual(reply['thread_id'], receipt['thread_id'])
            self.assertEqual(calls[-1][0], 'send')
            self.assertEqual(calls[-1][2], receipt['thread_id'])
            task['thread'] = {'adapter': 'webhook', 'thread_id': 'the_original_thread'}
            prior = len(calls)
            with self.assertRaises(DeliveryError):
                adapter.deliver(sample_event('dialogue.reply'), task)
            self.assertEqual(len(calls), prior)

    def test_desktop_errors_keep_the_delivery_uncertainty(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            class Client:
                def create(self, *_):
                    raise DesktopError('desktop_timeout', '桌面操作超时。', uncertain=True)

            adapter = DesktopAdapter({'desktop_binding_file': str(root/'binding.json')},
                data_dir=root/'state', project_dir=root, cli_path=root/'bridge.py', client=Client())
            with self.assertRaises(DeliveryError) as error:
                adapter.deliver(sample_event(), sample_task())
            self.assertTrue(error.exception.uncertain)

    def test_unknown_adapter_is_not_silently_demonstration(self):
        with self.assertRaises(DeliveryError):
            make_adapter({'adapter': 'typo'}, data_dir=Path('.'), project_dir=Path('.'),
                         cli_path=Path('bridge.py'))


if __name__ == '__main__':
    unittest.main()
