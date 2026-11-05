import base64
import json
import io
import re
import tempfile
import time
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.contrib.auth.models import Permission, User
from django.db import DatabaseError, connection
from django.test import Client, TestCase
from django.utils import timezone
from PIL import Image

from .forms import ChannelForm, ImmediateForm, MerchantForm, NotificationForm, RefundForm, ReplyForm
from .models import ChannelNotification, Complaint, Job, Merchant, Operation, WechatChannel
from .services import perform_operation, process_job, receive_channel_notification, sync_one, sync_range
from .wechat import WechatError, WechatGateway, configuration_errors


class ChannelIntegrationTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.directory = tempfile.TemporaryDirectory()
        cls.keys = {}
        cls.paths = {}
        for name in ('channel', 'direct', 'wechat'):
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            cls.keys[name] = key
            path = Path(cls.directory.name) / (name + '.pem')
            raw = (key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
                   if name == 'wechat' else key.private_bytes(serialization.Encoding.PEM,
                       serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            path.write_bytes(raw)
            path.chmod(0o600)
            cls.paths[name] = str(path)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()
        super().tearDownClass()

    def setUp(self):
        self.channel = WechatChannel.objects.create(name='测试渠道', provider='测试支付机构', mch_id='1990000100',
            serial_no='CHANNEL-SERIAL', private_key_path=self.paths['channel'], api_v3_key='c' * 32,
            public_key_id='PUB_KEY_ID_CHANNEL', public_key_path=self.paths['wechat'])
        self.merchant = Merchant.objects.create(name='渠道子商户', mch_id='1990000101',
            access_mode='channel', channel=self.channel, provider_merchant_id='399TEST001')
        self.direct = Merchant.objects.create(name='原自行调用商户', mch_id='1990000200',
            serial_no='DIRECT-SERIAL', private_key_path=self.paths['direct'], api_v3_key='d' * 32,
            public_key_id='PUB_KEY_ID_DIRECT', public_key_path=self.paths['wechat'])
        self.user = User.objects.create_superuser('channel-admin', '', 'LocalTestOnly!9834')
        self.client.force_login(self.user)

    def headers(self, body, key_id='PUB_KEY_ID_CHANNEL'):
        stamp, nonce = str(int(time.time())), 'CHANNEL-NONCE'
        signature = self.keys['wechat'].sign(stamp.encode() + b'\n' + nonce.encode() + b'\n' + body + b'\n',
                                             padding.PKCS1v15(), hashes.SHA256())
        return {'Wechatpay-Timestamp': stamp, 'Wechatpay-Nonce': nonce, 'Wechatpay-Serial': key_id,
                'Wechatpay-Signature': base64.b64encode(signature).decode(), 'Request-ID': 'CHANNEL-REQUEST'}

    def response(self, data=None, status=200, key_id='PUB_KEY_ID_CHANNEL'):
        body = json.dumps(data, ensure_ascii=False).encode() if data is not None else b''
        return httpx.Response(status, content=body, headers=self.headers(body, key_id))

    def detail(self, cid='CHANNEL-C001', mch_id=None, **extra):
        return {'complaint_id': cid, 'complainted_mchid': mch_id or self.merchant.mch_id,
                'complaint_state': 'PROCESSING', 'complaint_time': timezone.now().isoformat(),
                'problem_type': 'OTHERS', 'complaint_order_info': [], **extra}

    def factory(self, handler):
        return lambda subject: WechatGateway(subject, transport=httpx.MockTransport(handler))

    def assert_signature(self, request, account, private_name, signing_body=None):
        parts = dict(re.findall(r'(\w+)="([^"]+)"', request.headers['Authorization']))
        self.assertEqual(parts['mchid'], account.mch_id)
        self.assertEqual(parts['serial_no'], account.serial_no)
        self.assertEqual(request.headers['Wechatpay-Serial'], account.public_key_id)
        body = request.content if signing_body is None else signing_body
        canonical = f'{request.method}\n{request.url.raw_path.decode()}\n{parts["timestamp"]}\n{parts["nonce_str"]}\n'.encode() + body + b'\n'
        self.keys[private_name].public_key().verify(base64.b64decode(parts['signature']), canonical,
                                                  padding.PKCS1v15(), hashes.SHA256())

    def callback_body(self, payload=None, event_id='CHANNEL-EVENT001'):
        nonce = 'abcdefghijkl'
        # 官方通知只有投诉号和动作类型，不依赖子商户号存在。
        raw = json.dumps(payload or {'complaint_id': 'CHANNEL-C001', 'action_type': 'USER_RESPONSE'}).encode()
        cipher = AESGCM(self.channel.api_v3_key.encode()).encrypt(nonce.encode(), raw, b'complaint')
        return json.dumps({'id': event_id, 'resource': {'algorithm': 'AEAD_AES_256_GCM', 'nonce': nonce,
            'associated_data': 'complaint', 'ciphertext': base64.b64encode(cipher).decode()}}).encode()

    def post_callback(self, body):
        headers = {'HTTP_' + k.upper().replace('-', '_'): v for k, v in self.headers(body).items()}
        return self.client.post(self.channel.callback_path, body, content_type='application/json', **headers)

    def test_channel_and_direct_keep_distinct_signers_with_one_request_each(self):
        for merchant, account, key in [(self.merchant, self.channel, 'channel'), (self.direct, self.direct, 'direct')]:
            calls = []
            def handle(request):
                calls.append(request)
                self.assert_signature(request, account, key)
                self.assertEqual(request.url.params['complainted_mchid'], merchant.mch_id)
                return self.response({'data': [], 'total_count': 0}, key_id=account.public_key_id)
            self.factory(handle)(merchant).list_complaints('2026-10-01', '2026-10-30')
            self.assertEqual(len(calls), 1)

    def test_channel_private_key_decrypts_sensitive_phone(self):
        data = self.detail()
        data['payer_phone'] = base64.b64encode(self.keys['channel'].public_key().encrypt(b'13800138000',
            padding.OAEP(mgf=padding.MGF1(hashes.SHA1()), algorithm=hashes.SHA1(), label=None))).decode()
        gw = self.factory(lambda request: self.response(data))(self.merchant)
        self.assertEqual(gw.complaint(data['complaint_id'])['payer_phone'], '13800138000')

    def test_channel_images_use_channel_multipart_signature_and_existing_download(self):
        image = io.BytesIO()
        Image.new('RGB', (3, 3), 'blue').save(image, format='PNG')
        raw = image.getvalue()
        calls = []
        def handle(request):
            calls.append(request)
            if request.method == 'POST':
                import hashlib
                meta = json.dumps({'filename': 'evidence.png', 'sha256': hashlib.sha256(raw).hexdigest()}, separators=(',', ':')).encode()
                self.assert_signature(request, self.channel, 'channel', signing_body=meta)
                self.assertIn(meta, request.content)
                return self.response({'media_id': 'CHANNEL/IMAGE=='})
            self.assert_signature(request, self.channel, 'channel')
            self.assertTrue(request.url.raw_path.endswith(b'CHANNEL%2FIMAGE%3D%3D'))
            return httpx.Response(200, content=raw)
        gw = self.factory(handle)(self.merchant)
        self.assertEqual(gw.upload('original.png', raw), 'CHANNEL/IMAGE==')
        self.assertEqual(gw.download('CHANNEL/IMAGE=='), (raw, 'image/png'))
        self.assertEqual(len(calls), 2)

    def test_channel_actions_reuse_real_service_and_post_only_once(self):
        for kind in ('reply', 'complete', 'immediate', 'refund'):
            cid = 'CHANNEL-' + kind
            data = self.detail(cid, problem_type='REFUND' if kind == 'refund' else 'OTHERS', need_immediate_service=True)
            complaint = Complaint.objects.create(merchant=self.merchant, complaint_id=cid, state='PROCESSING', detail=data)
            key = uuid.uuid4()
            if kind == 'reply':
                form = ReplyForm({'operation_id': key, 'content': '收到，我们会跟进'}, complaint=complaint)
                self.assertTrue(form.is_valid(), form.errors)
                payload = form.payload()
            elif kind == 'immediate':
                form = ImmediateForm({'operation_id': key, 'content': '人工已接入'}, complaint=complaint)
                self.assertTrue(form.is_valid(), form.errors)
                payload = form.payload()
            elif kind == 'refund':
                form = RefundForm({'operation_id': key, 'action': 'APPROVE', 'launch_refund_day': 1,
                                   'acknowledged': True}, complaint=complaint)
                self.assertTrue(form.is_valid(), form.errors)
                payload = form.payload()
            else:
                payload = {'complainted_mchid': self.merchant.mch_id}
            calls = []
            def handle(request):
                calls.append(request)
                self.assert_signature(request, self.channel, 'channel')
                if request.method == 'POST':
                    self.assertEqual(json.loads(request.content), payload)
                    if kind != 'refund': self.assertEqual(payload['complainted_mchid'], self.merchant.mch_id)
                    return self.response(None, 204)
                return self.response({'data': [], 'total_count': 0} if request.url.path.endswith('negotiation-historys') else data)
            with patch('desk.services.gateway_for', side_effect=self.factory(handle)):
                op, _ = perform_operation(self.merchant, complaint, self.user, key, kind, payload)
                self.assertEqual(op.state, 'success', op.result)
                before_replay = len(calls)
                perform_operation(self.merchant, complaint, self.user, key, kind, payload)
            self.assertEqual(len(calls), before_replay)
            self.assertEqual([call.method for call in calls], ['GET', 'POST', 'GET', 'GET'])

    def test_callback_is_durable_deduplicated_and_routes_after_ack(self):
        body = self.callback_body()
        with patch('httpx.Client.send') as send:
            self.assertEqual(self.post_callback(body).status_code, 204)
            self.assertEqual(self.post_callback(body).status_code, 204)
            send.assert_not_called()
        self.assertEqual(ChannelNotification.objects.count(), 1)
        job = Job.objects.get()
        self.assertEqual(job.channel_id, self.channel.pk)
        self.assertIsNone(job.merchant_id)
        calls = []
        def handle(request):
            calls.append(request)
            self.assert_signature(request, self.channel, 'channel')
            return self.response({'data': [], 'total_count': 0} if request.url.path.endswith('negotiation-historys') else self.detail())
        with patch('desk.services.gateway_for', side_effect=self.factory(handle)):
            self.assertTrue(process_job())
        self.assertEqual(Complaint.objects.get().merchant_id, self.merchant.pk)
        self.assertEqual(len(calls), 2)  # 详情一次、协商历史一次，路由后不重复查询详情。
        job.refresh_from_db()
        self.assertEqual(job.state, 'done')
        self.assertIsNone(job.dedupe_key)

    def test_callback_tamper_wrong_key_and_storage_failure_do_not_ack(self):
        body = self.callback_body()
        headers = {'HTTP_' + k.upper().replace('-', '_'): v for k, v in self.headers(body).items()}
        self.assertEqual(self.client.post(self.channel.callback_path, body + b' ', content_type='application/json', **headers).status_code, 400)
        headers['HTTP_WECHATPAY_SIGNATURE'] = 'WECHATPAY/SIGNTEST/invalid'
        self.assertEqual(self.client.post(self.channel.callback_path, body, content_type='application/json', **headers).status_code, 400)
        with patch('desk.views.receive_channel_notification', side_effect=DatabaseError()):
            self.assertEqual(self.post_callback(body).status_code, 503)
        self.assertEqual(ChannelNotification.objects.count(), 0)
        self.assertEqual(Job.objects.count(), 0)
        self.assertEqual(self.client.post(self.merchant.callback_path, body, content_type='application/json').status_code, 404)

    def test_notification_and_job_insert_are_atomic(self):
        with patch('desk.services.Job.objects.create', side_effect=DatabaseError()):
            with self.assertRaises(DatabaseError):
                receive_channel_notification(self.channel, 'ATOMIC-EVENT', {'complaint_id': 'C-ATOMIC'})
        self.assertEqual(ChannelNotification.objects.count(), 0)

    def test_other_channel_and_unconfigured_submerchant_are_not_imported(self):
        for mch_id in (self.direct.mch_id, '1999999999'):
            job = Job.objects.create(channel=self.channel, kind='channel_detail', payload={'complaint_id': 'C-UNLINKED'})
            calls = []
            def handle(request):
                calls.append(request)
                return self.response(self.detail('C-UNLINKED', mch_id=mch_id))
            with patch('desk.services.gateway_for', side_effect=self.factory(handle)):
                process_job()
            job.refresh_from_db()
            self.assertEqual(job.state, 'done')
            self.assertIn('未导入', job.error)
            self.assertEqual(len(calls), 1)
        self.assertFalse(Complaint.objects.exists())

    def test_channel_requires_exact_submerchant_before_sync_or_write(self):
        complaint = Complaint.objects.create(merchant=self.merchant, complaint_id='CHANNEL-C001')
        for detail in (self.detail(mch_id=self.direct.mch_id), {'complaint_id': 'CHANNEL-C001'}):
            calls = []
            def handle(request):
                calls.append(request)
                return self.response(detail)
            factory = self.factory(handle)
            with self.assertRaises(WechatError): sync_one(self.merchant, complaint.complaint_id, factory(self.merchant))
            with patch('desk.services.gateway_for', side_effect=factory):
                op, _ = perform_operation(self.merchant, complaint, self.user, uuid.uuid4(), 'reply',
                                          {'complainted_mchid': self.merchant.mch_id, 'response_content': '收到'})
            self.assertEqual(op.state, 'failed')
            self.assertTrue(all(call.method == 'GET' for call in calls))

    def test_channel_form_saves_reference_and_ignores_merchant_credentials(self):
        form = MerchantForm({'name': '新子商户', 'mch_id': '1990000102', 'access_mode': 'channel',
            'channel': self.channel.pk, 'provider_merchant_id': '399TEST002', 'api_v3_key_input': 'INVALID',
            'private_key_path': 'wrong-local-key', 'notify_url': 'https://wrong.example/callbacks/'})
        self.assertTrue(form.is_valid(), form.errors)
        merchant = form.save()
        self.assertEqual(merchant.channel_id, self.channel.pk)
        self.assertEqual(merchant.api_v3_key, '')
        self.assertEqual(merchant.private_key_path, '')
        self.assertEqual(configuration_errors(merchant), [])
        self.assertEqual(MerchantForm({'name': '旧格式', 'mch_id': '1990000999'}).is_valid(), True)
        self.assertFalse(MerchantForm({'name': '无渠道', 'mch_id': '1990000103', 'access_mode': 'channel'}).is_valid())

    def test_channel_secret_encrypted_blank_edit_retains_and_views_do_not_leak(self):
        with connection.cursor() as cursor:
            cursor.execute('SELECT api_v3_key FROM desk_wechatchannel WHERE id=%s', [self.channel.pk])
            self.assertNotIn('c' * 32, cursor.fetchone()[0])
        form_data = {name: getattr(self.channel, name) for name in ChannelForm.Meta.fields if name != 'api_v3_key_input'}
        form = ChannelForm(form_data, instance=self.channel)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.api_v3_key, 'c' * 32)
        response = self.client.get(f'/channels/{self.channel.pk}/edit/')
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'c' * 32)
        with patch('httpx.Client.send') as send:
            self.assertEqual(self.client.get('/channels/').status_code, 200)
            self.assertEqual(self.client.get(f'/merchants/{self.merchant.pk}/').status_code, 200)
            send.assert_not_called()

    def test_disabled_channel_demo_and_historical_identity_changes_are_rejected(self):
        self.channel.enabled = False
        self.channel.save()
        self.assertTrue(configuration_errors(self.merchant))
        self.assertEqual(configuration_errors(self.direct), [])
        form = MerchantForm({'name': '新商户', 'mch_id': '1990000104', 'access_mode': 'channel', 'channel': self.channel.pk})
        self.assertFalse(form.is_valid())
        self.channel.enabled = True
        self.channel.save()
        Complaint.objects.create(merchant=self.direct, complaint_id='DIRECT-C001')
        form = MerchantForm({'name': self.direct.name, 'mch_id': self.direct.mch_id,
            'access_mode': 'channel', 'channel': self.channel.pk}, instance=self.direct)
        self.assertFalse(form.is_valid())
        self.assertIn('access_mode', form.errors)

    def test_permissions_csrf_and_channel_delete_protection(self):
        reader = User.objects.create_user('channel-reader', password='LocalTestOnly!9834')
        reader.user_permissions.add(Permission.objects.get(codename='view_merchant'))
        self.client.force_login(reader)
        for url in ('/channels/', '/channels/new/', f'/channels/{self.channel.pk}/edit/', f'/channels/{self.channel.pk}/notifications/'):
            self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.user)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post('/channels/new/', {}).status_code, 403)
        self.client.post(f'/channels/{self.channel.pk}/delete/')
        self.assertTrue(WechatChannel.objects.filter(pk=self.channel.pk).exists())

    def test_channel_notification_configuration_requires_query_and_detects_changes(self):
        url = f'/channels/{self.channel.pk}/notifications/'
        new_url = 'https://new.example' + self.channel.callback_path
        remote = {'url': 'https://existing.example/complaint-notify', 'mchid': self.channel.mch_id}
        calls = []
        def handle(request):
            calls.append(request)
            self.assert_signature(request, self.channel, 'channel')
            if request.method == 'PUT': remote['url'] = json.loads(request.content)['url']
            return self.response(remote)
        factory = self.factory(handle)
        with patch('desk.views.gateway_for', side_effect=factory), patch('desk.services.gateway_for', side_effect=factory):
            data = {'action': 'PUT', 'url': new_url, 'confirmed': True, 'operation_id': uuid.uuid4()}
            response = self.client.post(url, data)
            self.assertContains(response, '请先查询微信当前通知配置')
            self.assertEqual(calls, [])
            response = self.client.post(url, {'action': 'GET', 'operation_id': uuid.uuid4()})
            self.assertContains(response, remote['url'])
            remote['url'] = 'https://changed.example/notify'
            response = self.client.post(url, data)
            self.assertContains(response, '微信通知配置已变化')
            self.assertFalse(any(call.method == 'PUT' for call in calls))
            self.client.post(url, {'action': 'GET', 'operation_id': uuid.uuid4()})
            response = self.client.post(url, data)
            self.assertContains(response, new_url)
        operation = Operation.objects.get()
        self.assertEqual(operation.channel_id, self.channel.pk)
        self.assertIsNone(operation.merchant_id)
        self.assertEqual(operation.state, 'success')
        self.assertEqual(len([call for call in calls if call.method == 'PUT']), 1)
        self.assertEqual(self.client.get(f'/merchants/{self.merchant.pk}/notifications/').url, url)
        with self.assertRaises(WechatError): factory(self.merchant).notification_config('PUT', new_url)

    def test_channel_notification_can_create_after_confirmed_empty_remote(self):
        url = f'/channels/{self.channel.pk}/notifications/'
        calls = []
        remote = {}
        def handle(request):
            calls.append(request)
            if request.method == 'GET' and not remote:
                return self.response({'code': 'RESOURCE_NOT_EXISTS'}, 404)
            if request.method == 'POST': remote['url'] = json.loads(request.content)['url']
            return self.response(remote)
        factory = self.factory(handle)
        with patch('desk.views.gateway_for', side_effect=factory), patch('desk.services.gateway_for', side_effect=factory):
            response = self.client.post(url, {'action': 'GET', 'operation_id': uuid.uuid4()})
            self.assertContains(response, '未配置通知地址')
            response = self.client.post(url, {'action': 'POST', 'confirmed': True, 'operation_id': uuid.uuid4(),
                'url': 'https://local-test.example' + self.channel.callback_path})
            self.assertContains(response, 'https://local-test.example' + self.channel.callback_path)
        self.assertEqual(len([request for request in calls if request.method == 'POST']), 1)

    def test_notification_url_boundaries_and_direct_validation_message_are_preserved(self):
        for subject in (self.direct, self.channel):
            data = {'action': 'PUT', 'confirmed': True, 'operation_id': uuid.uuid4(),
                    'url': 'https://example.com' + subject.callback_path}
            self.assertTrue(NotificationForm(data, merchant=subject).is_valid())
            data['url'] += '?unexpected=1'
            self.assertFalse(NotificationForm(data, merchant=subject).is_valid())
        data['url'] = 'https://example.com' + self.channel.callback_path
        form = NotificationForm(data, merchant=self.direct)
        self.assertFalse(form.is_valid())
        self.assertIn('/callbacks/当前商户号/', form.errors['url'][0])

    def test_failed_channel_notification_has_bounded_retry_and_can_be_requeued(self):
        job = Job.objects.create(channel=self.channel, kind='channel_detail', payload={'complaint_id': 'RETRY-C001'}, attempts=4)
        with patch('desk.services.gateway_for', side_effect=WechatError('渠道暂不可用')):
            self.assertTrue(process_job())
        job.refresh_from_db()
        self.assertEqual((job.state, job.attempts), ('failed', 5))
        self.assertIsNone(job.dedupe_key)
        url = f'/channels/{self.channel.pk}/jobs/{job.pk}/retry/'
        self.client.post(url)
        self.client.post(url)
        self.assertEqual(Job.objects.filter(channel=self.channel, state='queued').count(), 1)

    def parent_response_gateway(self, detail, rows_by_merchant, calls, *, total_count=None, action_check=None):
        """只替换HTTP传输，签名、验签、分页及业务归属判断都使用工程实现。"""
        def handle(request):
            calls.append(request)
            self.assert_signature(request, self.channel, 'channel')
            if request.method == 'POST':
                if action_check:
                    action_check(request)
                return self.response(None, 204)
            if request.url.path == '/v3/merchant-service/complaints-v2':
                owner = request.url.params['complainted_mchid']
                rows = rows_by_merchant(owner, int(request.url.params.get('offset', 0)))
                return self.response({'data': rows, 'total_count': len(rows) if total_count is None else total_count})
            if request.url.path.endswith('/negotiation-historys'):
                return self.response({'data': [], 'total_count': 0})
            return self.response(detail)
        return self.factory(handle)

    def test_channel_number_bulk_sync_keeps_filtered_submerchant_and_original_response(self):
        detail = self.detail(mch_id=self.channel.mch_id)
        calls = []
        factory = self.parent_response_gateway(detail, lambda owner, offset: [detail], calls)
        with patch('desk.services.gateway_for', side_effect=factory):
            self.assertEqual(sync_range(self.merchant, '2026-10-01', '2026-10-30'), 1)
        complaint = Complaint.objects.get()
        self.assertEqual(complaint.merchant_id, self.merchant.pk)
        self.assertEqual(complaint.detail['complainted_mchid'], self.channel.mch_id)
        self.assertEqual(len(calls), 3)  # 已有筛选列表作为依据，不再额外查询。
        self.assertEqual(calls[0].url.params['complainted_mchid'], self.merchant.mch_id)
        self.merchant.refresh_from_db()
        self.assertEqual(self.merchant.last_sync_error, '')
        self.assertIsNotNone(self.merchant.last_synced_at)

    def test_channel_number_single_sync_checks_local_day_and_membership_pagination(self):
        detail = self.detail(mch_id=self.channel.mch_id, complaint_time='2026-10-02T16:30:00Z')
        calls = []
        other_rows = [self.detail('OTHER-' + str(index), mch_id=self.channel.mch_id) for index in range(50)]
        factory = self.parent_response_gateway(detail,
            lambda owner, offset: other_rows if offset == 0 else [detail], calls, total_count=51)
        sync_one(self.merchant, detail['complaint_id'], factory(self.merchant))
        pages = [request for request in calls if request.url.path == '/v3/merchant-service/complaints-v2']
        self.assertEqual([request.url.params['offset'] for request in pages], ['0', '50'])
        for request in pages:
            self.assertEqual(request.url.params['begin_date'], '2026-10-03')
            self.assertEqual(request.url.params['end_date'], '2026-10-03')
            self.assertEqual(request.url.params['complainted_mchid'], self.merchant.mch_id)
        self.assertEqual(Complaint.objects.get().merchant_id, self.merchant.pk)

    def test_channel_number_without_filtered_membership_is_not_imported_or_written(self):
        detail = self.detail(mch_id=self.channel.mch_id)
        calls = []
        factory = self.parent_response_gateway(detail, lambda owner, offset: [], calls)
        with self.assertRaises(WechatError):
            sync_one(self.merchant, detail['complaint_id'], factory(self.merchant))
        self.assertFalse(Complaint.objects.exists())
        complaint = Complaint.objects.create(merchant=self.merchant, complaint_id=detail['complaint_id'], detail=detail)
        with patch('desk.services.gateway_for', side_effect=factory):
            operation, _ = perform_operation(self.merchant, complaint, self.user, uuid.uuid4(), 'reply',
                {'complainted_mchid': self.merchant.mch_id, 'response_content': '模拟回复'})
        self.assertEqual(operation.state, 'failed')
        self.assertTrue(all(request.method == 'GET' for request in calls))

    def test_channel_number_rejects_foreign_list_owner_and_wrong_query_context(self):
        detail = self.detail(mch_id=self.channel.mch_id)
        for row in (self.detail(mch_id=self.direct.mch_id), self.detail('WRONG-ID', mch_id=self.channel.mch_id)):
            factory = self.parent_response_gateway(detail, lambda owner, offset: [row], [])
            with self.assertRaises(WechatError):
                sync_one(self.merchant, detail['complaint_id'], factory(self.merchant), scoped_row=row)
        factory = self.parent_response_gateway(detail, lambda owner, offset: [detail], [])
        with self.assertRaisesMessage(WechatError, '查询上下文'):
            sync_one(self.merchant, detail['complaint_id'], factory(self.channel), scoped_row=detail)
        self.assertFalse(Complaint.objects.exists())

    def test_channel_number_cannot_reassign_complaint_already_bound_to_sibling(self):
        sibling = Merchant.objects.create(name='同渠道另一商户', mch_id='1990000102', access_mode='channel', channel=self.channel)
        detail = self.detail(mch_id=self.channel.mch_id)
        Complaint.objects.create(merchant=sibling, complaint_id=detail['complaint_id'], detail=detail)
        factory = self.parent_response_gateway(detail, lambda owner, offset: [detail], [])
        with self.assertRaisesMessage(WechatError, '已关联其他子商户'):
            sync_one(self.merchant, detail['complaint_id'], factory(self.merchant))
        self.assertEqual(list(Complaint.objects.values_list('merchant_id', flat=True)), [sibling.pk])

    def test_channel_range_continues_after_complaint_bound_to_sibling(self):
        sibling = Merchant.objects.create(name='同渠道另一商户', mch_id='1990000102', access_mode='channel', channel=self.channel)
        Complaint.objects.create(merchant=sibling, complaint_id='BOUND-C001')
        class Gateway:
            merchant = self.merchant
            def list_complaints(gw, begin, end, offset):
                return {'data': [gw.complaint('BOUND-C001'), gw.complaint('NEXT-C002')], 'total_count': 2}
            def complaint(gw, cid): return self.detail(cid, mch_id=self.channel.mch_id)
            def negotiations(gw, cid, offset=0): return {'data': [], 'total_count': 0}
        with patch('desk.services.gateway_for', return_value=Gateway()):
            with self.assertRaisesMessage(WechatError, 'BOUND-C001'):
                sync_range(self.merchant, '2026-10-01', '2026-10-01')
        self.assertTrue(Complaint.objects.filter(merchant=self.merchant, complaint_id='NEXT-C002').exists())
        self.assertEqual(Complaint.objects.get(complaint_id='BOUND-C001').merchant_id, sibling.pk)
        self.merchant.refresh_from_db()
        self.assertIsNone(self.merchant.last_synced_at)

    def test_channel_notification_resolution_waits_five_minutes(self):
        for state in ('pending', 'unknown'):
            with self.subTest(state=state):
                op = Operation.objects.create(channel=self.channel, user=self.user, kind='notify:PUT', payload_hash='x', state=state)
                url = f'/operations/{op.pk}/resolve/'
                response = self.client.post(url, {'outcome': 'failed', 'confirmed': 'on'}, follow=True)
                self.assertContains(response, '请等待至少5分钟后核对')
                op.refresh_from_db(); self.assertEqual(op.state, state)
                Operation.objects.filter(pk=op.pk).update(created_at=timezone.now() - timedelta(minutes=6))
                self.client.post(url, {'outcome': 'failed', 'confirmed': 'on'})
                op.refresh_from_db(); self.assertEqual(op.state, 'failed')

    def test_channel_number_callback_routes_only_after_unique_submerchant_match(self):
        sibling = Merchant.objects.create(name='回调目标商户', mch_id='1990000102', access_mode='channel', channel=self.channel)
        detail = self.detail(mch_id=self.channel.mch_id)
        calls = []
        factory = self.parent_response_gateway(detail,
            lambda owner, offset: [detail] if owner == sibling.mch_id else [], calls)
        with patch('httpx.Client.send') as send:
            self.assertEqual(self.post_callback(self.callback_body()).status_code, 204)
            send.assert_not_called()
        with patch('desk.services.gateway_for', side_effect=factory):
            self.assertTrue(process_job())
        self.assertEqual(Complaint.objects.get().merchant_id, sibling.pk)
        self.assertEqual(Job.objects.get().state, 'done')
        pages = [request for request in calls if request.url.path == '/v3/merchant-service/complaints-v2']
        self.assertEqual([request.url.params['complainted_mchid'] for request in pages],
                         [self.merchant.mch_id, sibling.mch_id])
        self.assertEqual(len(calls), 4)  # 一次详情、两次筛选、一次历史。

    def test_channel_number_callback_ambiguous_or_missing_scope_remains_retryable(self):
        Merchant.objects.create(name='另一商户', mch_id='1990000102', access_mode='channel', channel=self.channel)
        detail = self.detail(mch_id=self.channel.mch_id)
        for rows in ([], [detail]):
            job = Job.objects.create(channel=self.channel, kind='channel_detail', payload={'complaint_id': detail['complaint_id']})
            factory = self.parent_response_gateway(detail, lambda owner, offset: rows, [])
            with patch('desk.services.gateway_for', side_effect=factory):
                process_job()
            job.refresh_from_db()
            self.assertEqual(job.state, 'queued')
            self.assertFalse(Complaint.objects.exists())
            job.delete()

    def test_channel_number_actions_use_verified_wechat_owner_and_preserve_idempotency(self):
        for kind in ('reply', 'complete', 'immediate', 'refund'):
            cid = 'PARENT-' + kind
            detail = self.detail(cid, mch_id=self.channel.mch_id,
                                 problem_type='REFUND' if kind == 'refund' else 'OTHERS', need_immediate_service=True)
            complaint = Complaint.objects.create(merchant=self.merchant, complaint_id=cid, detail=detail)
            payload = ({'action': 'APPROVE', 'launch_refund_day': 0} if kind == 'refund' else
                       {'complainted_mchid': self.merchant.mch_id, **({'response_content': '模拟回复'} if kind == 'reply' else
                        {'message': {'blocks': [{'type': 'TEXT', 'text': {'text': '模拟即时回复'}}], 'sender_identity': 'MANUAL'}} if kind == 'immediate' else {})})
            expected = dict(payload)
            if kind != 'refund':
                expected['complainted_mchid'] = self.channel.mch_id
            calls = []
            factory = self.parent_response_gateway(detail, lambda owner, offset: [detail], calls,
                action_check=lambda request: self.assertEqual(json.loads(request.content), expected))
            key = uuid.uuid4()
            with patch('desk.services.gateway_for', side_effect=factory):
                operation, _ = perform_operation(self.merchant, complaint, self.user, key, kind, payload)
                self.assertEqual(operation.state, 'success', operation.result)
                before_replay = len(calls)
                perform_operation(self.merchant, complaint, self.user, key, kind, payload)
            self.assertEqual(len(calls), before_replay)
            self.assertEqual([request.method for request in calls], ['GET', 'GET', 'POST', 'GET', 'GET'])
            if kind != 'refund':
                self.assertEqual(payload['complainted_mchid'], self.merchant.mch_id)
            complaint.refresh_from_db()
            self.assertEqual(complaint.merchant_id, self.merchant.pk)
            self.assertEqual(complaint.detail['complainted_mchid'], self.channel.mch_id)
