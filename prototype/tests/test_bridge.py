import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib import error, request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as crew


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        crew.DB = Path(self.tmp.name) / 'crew.sqlite3'
        crew.BOOTSTRAP_SECRET = 'strong-test-secret'
        crew.setup()
        self.srv = crew.ThreadingHTTPServer(('127.0.0.1', 0), crew.CrewHandler)
        self.host = 'http://127.0.0.1:' + str(self.srv.server_port)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=3)
        self.tmp.cleanup()

    def req(self, path, token=None, data=None, method=None):
        headers = {}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        if data is not None:
            headers['Content-Type'] = 'application/json'
            data = json.dumps(data, ensure_ascii=False).encode('utf-8')
        r = request.Request(self.host + path, method=method or ('POST' if data is not None else 'GET'), data=data, headers=headers)
        try:
            with request.urlopen(r, timeout=2) as resp:
                return resp.status, json.loads(resp.read())
        except error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def accounts(self):
        stat, owner = self.req('/api/bootstrap', data={'secret': 'strong-test-secret', 'name': '玖玖'})
        self.assertEqual(stat, 201)
        stat, invitation = self.req('/api/invites', owner['token'], {})
        self.assertEqual(stat, 201)
        stat, aster = self.req('/api/register', data={'name': 'Aster', 'invite': invitation['invite']})
        self.assertEqual(stat, 201)
        stat, invitation2 = self.req('/api/invites', owner['token'], {})
        self.assertEqual(stat, 201)
        stat, jindu = self.req('/api/register', data={'name': '津渡', 'invite': invitation2['invite']})
        self.assertEqual(stat, 201)
        return owner, aster, jindu

    def test_initial_owner_invites_and_identity(self):
        owner, aster, jindu = self.accounts()
        self.assertEqual(self.req('/api/bootstrap', data={'secret': 'strong-test-secret', 'name': '第二群主'})[0], 409)
        self.assertEqual(self.req('/api/members')[0], 401)
        self.assertEqual(self.req('/api/invites', aster['token'], {})[0], 403)
        self.assertEqual(self.req('/api/me', aster['token'])[1]['member']['name'], 'Aster')
        self.assertEqual(self.req('/api/members', jindu['token'])[1]['members'][0]['name'], '玖玖')

    def test_two_real_member_accounts_cross_message(self):
        owner, aster, jindu = self.accounts()
        status, m = self.req('/api/rooms/lobby/messages', jindu['token'], {'body': '@Aster 帮我看一下接口'})
        self.assertEqual(status, 201)
        self.assertEqual(m['queued_for'], 1)
        self.assertEqual(m['delivery'], 'inbox_only')
        note = self.req('/api/inbox?after=0', aster['token'])[1]['notifications']
        self.assertEqual(len(note), 1)
        self.assertEqual(note[0]['sender_name'], '津渡')
        self.assertEqual(self.req('/api/inbox?after='+str(note[0]['seq']), aster['token'])[1]['notifications'], [])
        self.assertEqual(self.req('/api/inbox?after=0', owner['token'])[1]['notifications'], [])
        result = self.req('/api/rooms/lobby/messages', aster['token'], {'body': '@津渡 我建议先写幂等测试'})
        self.assertEqual(result[0], 201)
        reply_notes = self.req('/api/inbox?after=0', jindu['token'])[1]['notifications']
        self.assertEqual(len(reply_notes), 1)
        self.assertEqual(reply_notes[0]['sender_name'], 'Aster')
        messages = self.req('/api/rooms/lobby/messages?after='+str(m['seq']), jindu['token'])[1]['messages']
        self.assertEqual(len(messages), 1)
        self.assertIn('幂等测试', messages[0]['body'])

    def test_assembly_broadcast_is_only_queued_not_wake(self):
        owner, aster, jindu = self.accounts()
        status, result = self.req('/api/rooms/lobby/assemble', jindu['token'], {'body': '今晚讨论跨项目协作'})
        self.assertEqual(status, 201)
        self.assertEqual(result['queued_for'], 2)
        self.assertEqual(result['delivery'], 'inbox_only')
        self.assertEqual(self.req('/api/inbox', jindu['token'])[1]['notifications'], [])
        self.assertEqual(self.req('/api/inbox', owner['token'])[1]['notifications'][0]['kind'], 'assembly')
        self.assertEqual(self.req('/api/inbox', aster['token'])[1]['notifications'][0]['kind'], 'assembly')

    def test_project_room_and_validation(self):
        owner, aster, jindu = self.accounts()
        status, data = self.req('/api/rooms', aster['token'], {'name': 'Cistella 接口对接'})
        self.assertEqual(status, 201)
        room = data['room']['id']
        self.assertEqual(self.req('/api/rooms/'+room+'/messages', aster['token'], {'body':'项目启动'})[0], 201)
        self.assertEqual(len(self.req('/api/rooms/'+room+'/messages', owner['token'])[1]['messages']), 1)
        self.assertEqual(self.req('/api/rooms/'+room+'/messages', owner['token'], {'body': ''})[0], 400)
        self.assertEqual(self.req('/api/rooms/'+room+'/messages?after=-2', owner['token'])[0], 400)
        self.assertEqual(self.req('/api/rooms/invalid/messages', owner['token'])[0], 404)


if __name__ == '__main__':
    unittest.main()