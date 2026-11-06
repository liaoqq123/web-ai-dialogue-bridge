import json
from pathlib import Path
import struct
import tempfile
import unittest

from dialogue_bridge.desktop import DesktopClient, DesktopError, _decode_frame, _encode_frame


def response(value):
    return {'result': {'success': True, 'contentItems': [
        {'type': 'inputText', 'text': json.dumps(value)}]}}


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.binding_file = self.root / 'binding.json'
        self.binding = {'schema_version': 1, 'project_id': 'project-example',
                        'project_path': str(self.project), 'host_id': 'local',
                        'caller_thread_id': 'caller-example',
                        'pipe_path': '\\\\.\\pipe\\example-app-tools'}
        self.binding_file.write_text(json.dumps(self.binding), encoding='utf-8')
        self.calls = []
        self.remote_project = 'project-example'
        self.remote_thread = 'thread-example'
        self.create_result = {'threadId': self.remote_thread}

    def requester(self, pipe, method, params, **kwargs):
        self.calls.append((params['tool'], params['arguments'], kwargs['mutating']))
        self.assertEqual(method, 'tools/call')
        self.assertEqual(pipe, self.binding['pipe_path'])
        self.assertEqual(params['threadId'], self.binding['caller_thread_id'])
        tool = params['tool']
        if tool == 'list_projects':
            return response({'projects': [{'projectId': self.remote_project,
                              'projectKind': 'local', 'hostId': 'local',
                              'path': str(self.project)}]})
        if tool == 'create_thread':
            return response(self.create_result)
        if tool == 'read_thread':
            return response({'thread': {'id': self.remote_thread, 'hostId': 'local',
                              'projectId': self.remote_project, 'cwd': str(self.project)}})
        if tool == 'send_message_to_thread':
            return response({'threadId': self.remote_thread, 'hostId': 'local'})
        raise AssertionError('unexpected tool')

    def client(self, requester=None):
        return DesktopClient(self.binding_file, self.project,
                             requester=requester or self.requester)

    def test_check_is_read_only_and_receipt_has_no_connection_secrets(self):
        result = self.client().check()
        self.assertTrue(result['available'])
        self.assertEqual([c[0] for c in self.calls], ['list_projects'])
        self.assertFalse(any(c[2] for c in self.calls))
        self.assertNotIn('pipe_path', result)
        self.assertNotIn('caller_thread_id', result)

    def test_create_belongs_to_exact_project(self):
        receipt = self.client().create('Analyze the uploaded data.', 'Example')
        self.assertEqual(receipt['thread_id'], self.remote_thread)
        create = [c for c in self.calls if c[0] == 'create_thread']
        self.assertEqual(len(create), 1)
        self.assertEqual(create[0][1]['target'], {'type': 'project',
            'projectId': self.binding['project_id'], 'environment': {'type': 'local'}})
        self.assertNotIn('pipe_path', receipt)
        self.assertNotIn('caller_thread_id', receipt)

    def test_reply_verifies_then_sends_only_to_original_chat(self):
        receipt = self.client().create('Read data.', 'Example')
        self.calls.clear()
        reply = self.client().send(self.remote_thread, 'The answer is 10.', receipt)
        self.assertEqual(reply['thread_id'], receipt['thread_id'])
        self.assertEqual([c[0] for c in self.calls],
                         ['list_projects', 'read_thread', 'send_message_to_thread'])
        self.assertEqual(self.calls[-1][1]['threadId'], receipt['thread_id'])

    def test_project_mismatch_is_rejected_before_creation(self):
        self.remote_project = 'another-project'
        with self.assertRaises(DesktopError) as error:
            self.client().create('Read.', 'Example')
        self.assertFalse(error.exception.uncertain)
        self.assertFalse(any(c[2] for c in self.calls))

    def test_reply_cannot_be_redirected_to_foreign_project(self):
        expected = {'project_id': 'another-project', 'project_path': str(self.project),
                    'host_id': 'local'}
        with self.assertRaises(DesktopError):
            self.client().send(self.remote_thread, 'Answer', expected)
        self.assertEqual(self.calls, [])

    def test_queued_identity_is_uncertain_not_recreated(self):
        self.create_result = {'clientThreadId': 'queued-example'}
        with self.assertRaises(DesktopError) as error:
            self.client().create('Read.', 'Example')
        self.assertTrue(error.exception.uncertain)
        self.assertEqual(sum(c[0] == 'create_thread' for c in self.calls), 1)

    def test_malformed_mutation_success_is_uncertain(self):
        def requester(pipe, method, params, **kwargs):
            if params['tool'] == 'create_thread':
                return {'result': {'success': True, 'contentItems': []}}
            return self.requester(pipe, method, params, **kwargs)
        with self.assertRaises(DesktopError) as error:
            self.client(requester).create('Read.', 'Example')
        self.assertTrue(error.exception.uncertain)

    def test_timeout_after_mutating_call_is_uncertain(self):
        def requester(pipe, method, params, **kwargs):
            if kwargs['mutating']:
                raise TimeoutError('private transport details')
            return self.requester(pipe, method, params, **kwargs)
        with self.assertRaises(DesktopError) as error:
            self.client(requester).create('Read.', 'Example')
        self.assertTrue(error.exception.uncertain)
        self.assertNotIn('private', str(error.exception))

    def test_binding_rotation_during_creation_stops_before_mutation(self):
        def requester(pipe, method, params, **kwargs):
            result = self.requester(pipe, method, params, **kwargs)
            if params['tool'] == 'list_projects':
                new = {**self.binding, 'caller_thread_id': 'another-caller'}
                self.binding_file.write_text(json.dumps(new), encoding='utf-8')
            return result
        with self.assertRaises(DesktopError) as error:
            self.client(requester).create('Read.', 'Example')
        self.assertFalse(error.exception.uncertain)
        self.assertFalse(any(c[2] for c in self.calls))

    def test_wrong_thread_metadata_prevents_reply(self):
        receipt = self.client().create('Read.', 'Example')
        self.remote_thread = 'different-thread'
        self.calls.clear()
        with self.assertRaises(DesktopError):
            self.client().send(receipt['thread_id'], 'Answer', receipt)
        self.assertFalse(any(c[2] for c in self.calls))

    def test_binding_cannot_choose_a_different_project_directory(self):
        another = self.root / 'other'
        another.mkdir()
        self.binding_file.write_text(json.dumps({**self.binding,
            'project_path': str(another)}), encoding='utf-8')
        with self.assertRaises(DesktopError):
            self.client().check()
        self.assertEqual(self.calls, [])

    def test_framing_rejects_truncation_and_nonobjects(self):
        value = {'text': '你好'}
        frame = _encode_frame(value)
        self.assertEqual(_decode_frame(frame), value)
        for bad in (frame[:-1], frame + b'x', b'\x00\x00\x00\x00',
                    struct.pack('<I', 2) + b'[]'):
            with self.subTest(bad=bad[:8]), self.assertRaises(DesktopError):
                _decode_frame(bad)


if __name__ == '__main__':
    unittest.main()
