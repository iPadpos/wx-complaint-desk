import base64
import hashlib
import io
import json
import os
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
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from PIL import Image

from .forms import ImmediateForm, MerchantForm, RefundForm, ReplyForm, SyncForm
from .models import Complaint, Job, Media, Merchant, Notification, Operation
from .services import enqueue, perform_operation, process_job, sync_one, sync_range
from .wechat import WechatError, WechatGateway, verify_signature


class WechatIntegrationTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.directory = tempfile.TemporaryDirectory()
        cls.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.platform = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.private_path = str(Path(cls.directory.name) / 'merchant.pem')
        cls.public_path = str(Path(cls.directory.name) / 'wechat.pem')
        Path(cls.private_path).write_bytes(cls.private.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        os.chmod(cls.private_path, 0o600)
        Path(cls.public_path).write_bytes(cls.platform.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()
        super().tearDownClass()

    def setUp(self):
        self.merchant = Merchant.objects.create(name='测试商户', mch_id='1900000011', serial_no='MERCHANT-SERIAL',
            private_key_path=self.private_path, api_v3_key='a' * 32, public_key_id='PUB_KEY_ID_TEST', public_key_path=self.public_path)
        self.other = Merchant.objects.create(name='另一商户', mch_id='1900000012')
        self.user = User.objects.create_superuser('admin', '', 'TestOnly!password-123')
        self.data = {'complaint_id': 'C001', 'complainted_mchid': self.merchant.mch_id,
            'complaint_state': 'PROCESSING', 'complaint_time': timezone.now().isoformat(),
            'complaint_detail': '测试敏感投诉正文', 'problem_type': 'OTHERS',
            'complaint_order_info': [{'out_trade_no': 'ORDER001', 'transaction_id': 'WX001', 'amount': 100}],
            'payer_phone': '13800138000', 'incoming_user_response': True, 'need_immediate_service': True}
        self.complaint = Complaint.objects.create(merchant=self.merchant, complaint_id='C001', state='PROCESSING', detail=self.data)

    def signed_headers(self, body, timestamp=None, key_id='PUB_KEY_ID_TEST'):
        stamp = str(timestamp or int(time.time()))
        nonce = 'TEST-NONCE'
        signature = base64.b64encode(self.platform.sign(stamp.encode() + b'\n' + nonce.encode() + b'\n' + body + b'\n',
            padding.PKCS1v15(), hashes.SHA256())).decode()
        return {'Wechatpay-Timestamp': stamp, 'Wechatpay-Nonce': nonce, 'Wechatpay-Serial': key_id,
                'Wechatpay-Signature': signature, 'Request-ID': 'TEST-REQUEST'}

    def response(self, data=None, status=200):
        body = json.dumps(data, ensure_ascii=False).encode() if data is not None else b''
        return httpx.Response(status, content=body, headers=self.signed_headers(body))

    def gw(self, handler): return WechatGateway(self.merchant, transport=httpx.MockTransport(handler))

    def test_get_request_signature_includes_encoded_query(self):
        def handle(request):
            import re
            parts = dict(re.findall(r'(\w+)="([^"]+)"', request.headers['Authorization']))
            canonical = f'GET\n{request.url.raw_path.decode()}\n{parts["timestamp"]}\n{parts["nonce_str"]}\n\n'.encode()
            self.private.public_key().verify(base64.b64decode(parts['signature']), canonical, padding.PKCS1v15(), hashes.SHA256())
            self.assertEqual(parts['mchid'], self.merchant.mch_id)
            self.assertEqual(request.url.params['complainted_mchid'], self.merchant.mch_id)
            return self.response({'data': [], 'total_count': 0})
        self.assertEqual(self.gw(handle).list_complaints('2026-09-01', '2026-09-30')['data'], [])

    def test_response_tamper_and_unknown_serial_are_rejected(self):
        def handle(request):
            resp = self.response({'data': []})
            resp._content = b'{"data":[1]}'
            return resp
        with self.assertRaises(WechatError): self.gw(handle).list_complaints('2026-09-01', '2026-09-30')
        with self.assertRaises(WechatError): verify_signature(self.merchant, self.signed_headers(b'', key_id='OTHER'), b'')
        with self.assertRaises(WechatError): verify_signature(self.merchant, self.signed_headers(b'', timestamp=int(time.time()) - 301), b'')

    def test_sensitive_phone_oaep_decryption(self):
        data = dict(self.data)
        data['payer_phone'] = base64.b64encode(self.private.public_key().encrypt(b'13800138000',
            padding.OAEP(mgf=padding.MGF1(hashes.SHA1()), algorithm=hashes.SHA1(), label=None))).decode()
        self.assertEqual(self.gw(lambda _: self.response(data)).complaint('C001')['payer_phone'], '13800138000')

    def callback_body(self):
        nonce = 'abcdefghijkl'
        data = json.dumps({'complaint_id': 'C001', 'action_type': 'USER_RESPONSE', 'complainted_mchid': self.merchant.mch_id}).encode()
        cipher = AESGCM(self.merchant.api_v3_key.encode()).encrypt(nonce.encode(), data, b'complaint')
        return json.dumps({'id': 'EVENT001', 'resource': {'algorithm': 'AEAD_AES_256_GCM', 'nonce': nonce,
            'associated_data': 'complaint', 'ciphertext': base64.b64encode(cipher).decode()}}).encode()

    def test_callback_valid_repeat_tamper_and_storage_failure(self):
        client = Client(enforce_csrf_checks=True)
        body = self.callback_body()
        headers = {'HTTP_' + k.upper().replace('-', '_'): v for k, v in self.signed_headers(body).items()}
        url = f'/callbacks/{self.merchant.mch_id}/'
        self.assertEqual(client.post(url, body, content_type='application/json', **headers).status_code, 204)
        self.assertEqual(client.post(url, body, content_type='application/json', **headers).status_code, 204)
        self.assertEqual(Notification.objects.count(), 1)
        self.assertEqual(Job.objects.count(), 1)
        self.assertEqual(Job.objects.get().payload, {'complaint_id': 'C001'})
        self.assertEqual(client.post(url, body + b' ', content_type='application/json', **headers).status_code, 400)
        from django.db import DatabaseError
        with patch('desk.views.receive_notification', side_effect=DatabaseError()):
            self.assertEqual(client.post(url, body, content_type='application/json', **headers).status_code, 503)

    def test_encryption_at_rest_and_key_edit_does_not_expose_secret(self):
        with connection.cursor() as cur:
            cur.execute('SELECT api_v3_key FROM desk_merchant WHERE id=%s', [self.merchant.pk])
            self.assertNotIn('a' * 32, cur.fetchone()[0])
            cur.execute('SELECT detail FROM desk_complaint WHERE id=%s', [self.complaint.pk])
            raw = cur.fetchone()[0]
            self.assertNotIn('13800138000', raw)
            self.assertNotIn('测试敏感投诉正文', raw)
        self.complaint.refresh_from_db()
        self.assertEqual(self.complaint.detail['payer_phone'], '13800138000')
        self.client.force_login(self.user)
        response = self.client.get(f'/merchants/{self.merchant.pk}/edit/')
        self.assertNotContains(response, 'a' * 32)
        data = {field: getattr(self.merchant, field) for field in MerchantForm.Meta.fields if field != 'api_v3_key_input'}
        data['api_v3_key_input'] = ''
        form = MerchantForm(data, instance=self.merchant)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.merchant.refresh_from_db()
        self.assertEqual(self.merchant.api_v3_key, 'a' * 32)

    def test_reply_unknown_replay_and_changed_payload(self):
        class UnknownGateway:
            request_id = ''
            calls = 0
            def complaint(gw, cid): return dict(self.data)
            def action(gw, *args):
                gw.calls += 1
                raise WechatError('超时', unknown=True)
        gw = UnknownGateway()
        key = uuid.uuid4()
        body = {'complainted_mchid': self.merchant.mch_id, 'response_content': '测试回复'}
        with patch('desk.services.gateway_for', return_value=gw):
            op, _ = perform_operation(self.merchant, self.complaint, self.user, key, 'reply', body)
            self.assertEqual(op.state, 'unknown')
            perform_operation(self.merchant, self.complaint, self.user, key, 'reply', body)
            self.assertEqual(gw.calls, 1)
            with self.assertRaises(WechatError): perform_operation(self.merchant, self.complaint, self.user, key, 'reply', {'other': 1})
            with self.assertRaises(WechatError): perform_operation(self.merchant, self.complaint, self.user, uuid.uuid4(), 'reply', body)

    def test_success_not_resent_when_refresh_fails(self):
        class Gateway:
            request_id = 'WRITE-SUCCESS'
            calls = 0
            def complaint(gw, cid): return dict(self.data)
            def action(gw, *args): gw.calls += 1
        gw = Gateway()
        key = uuid.uuid4()
        body = {'complainted_mchid': self.merchant.mch_id, 'response_content': '收到'}
        with patch('desk.services.gateway_for', return_value=gw), patch('desk.services.sync_one', side_effect=WechatError('读取失败')):
            op, msg = perform_operation(self.merchant, self.complaint, self.user, key, 'reply', body)
            self.assertEqual(op.state, 'success')
            self.assertIn('回查暂未完成', msg)
            self.assertEqual(Job.objects.count(), 1)
            perform_operation(self.merchant, self.complaint, self.user, key, 'reply', body)
            self.assertEqual(gw.calls, 1)

    def test_complete_pending_and_refund_complaints_are_rejected(self):
        class Gateway:
            calls = 0
            def complaint(gw, cid): return dict(self.data)
            def action(gw, *args): gw.calls += 1
        gw = Gateway()
        with patch('desk.services.gateway_for', return_value=gw):
            self.data['complaint_state'] = 'PENDING'
            op, _ = perform_operation(self.merchant, self.complaint, self.user, uuid.uuid4(), 'complete', {})
            self.assertEqual(op.state, 'failed')
            self.data.update(complaint_state='PROCESSING', problem_type='REFUND')
            op, _ = perform_operation(self.merchant, self.complaint, self.user, uuid.uuid4(), 'complete', {})
            self.assertEqual(op.state, 'failed')
            self.assertEqual(gw.calls, 0)

    def test_upload_signature_uses_meta_and_download_escapes_media_id(self):
        buf = io.BytesIO(); Image.new('RGB', (3, 3), 'green').save(buf, format='PNG'); raw = buf.getvalue()
        def handle(request):
            if request.method == 'POST':
                import re
                parts = dict(re.findall(r'(\w+)="([^"]+)"', request.headers['Authorization']))
                meta = json.dumps({'filename': 'evidence.png', 'sha256': hashlib.sha256(raw).hexdigest()}, separators=(',', ':')).encode()
                canonical = f'POST\n{request.url.raw_path.decode()}\n{parts["timestamp"]}\n{parts["nonce_str"]}\n'.encode() + meta + b'\n'
                self.private.public_key().verify(base64.b64decode(parts['signature']), canonical, padding.PKCS1v15(), hashes.SHA256())
                self.assertIn(meta, request.content)
                return self.response({'media_id': 'A/B=='})
            self.assertTrue(request.url.raw_path.endswith(b'A%2FB%3D%3D'))
            return httpx.Response(200, content=raw, headers={'Content-Type': 'image/png'})
        gw = self.gw(handle)
        self.assertEqual(gw.upload('fake-path.png', raw), 'A/B==')
        self.assertEqual(gw.download('A/B=='), (raw, 'image/png'))
        with self.assertRaises(WechatError): gw.upload('bad.jpg', b'not an image')

    def test_full_sync_pagination_and_merchant_ownership(self):
        class Gateway:
            def complaint(gw, cid): return dict(self.data)
            def negotiations(gw, cid, offset=0):
                return {'total_count': 301, 'data': [{'operate_type': 'USER_RESPONSE', 'operate_details': str(i)} for i in range(offset, min(301, offset + 300))]}
        row = sync_one(self.merchant, 'C001', Gateway())
        self.assertEqual(len(row.history), 301)
        self.assertEqual(row.order_no, 'ORDER001')
        self.data['complainted_mchid'] = self.other.mch_id
        with self.assertRaises(WechatError): sync_one(self.merchant, 'C001', Gateway())

    def test_range_splits_into_30_day_batches(self):
        calls = []
        class Gateway:
            def list_complaints(gw, begin, end, offset):
                calls.append((begin, end)); return {'total_count': 0, 'data': []}
        with patch('desk.services.gateway_for', return_value=Gateway()):
            sync_range(self.merchant, '2026-08-01', '2026-09-01')
        self.assertEqual(calls, [('2026-08-01', '2026-08-30'), ('2026-08-31', '2026-09-01')])

    def test_native_date_inputs_use_iso_format_with_chinese_locale(self):
        form = SyncForm(initial={'begin': timezone.localdate(), 'end': timezone.localdate()})
        self.assertIn(f'value="{timezone.localdate().isoformat()}"', str(form['begin']))
        self.assertIn('type="date"', str(form['end']))

    def test_job_retry_dedup_and_persisted_work(self):
        first = enqueue(self.merchant, 'detail', {'complaint_id': 'C001'}, 'one')
        self.assertEqual(first.pk, enqueue(self.merchant, 'detail', {'complaint_id': 'C001'}, 'one').pk)
        with patch('desk.services.sync_one', side_effect=WechatError('暂时失败')):
            self.assertTrue(process_job())
        first.refresh_from_db()
        self.assertEqual(first.attempts, 1)
        self.assertEqual(first.state, 'queued')
        first.available_at = timezone.now(); first.save()
        with patch('desk.services.sync_one') as call:
            self.assertTrue(process_job())
            self.assertEqual(call.call_args.args[0].pk, self.merchant.pk)
        first.refresh_from_db()
        self.assertEqual(first.state, 'done')
        self.assertIsNone(first.dedupe_key)

    def test_permission_csrf_cross_merchant_and_protected_delete(self):
        user = User.objects.create_user('reader', password='TestOnly-12345')
        user.user_permissions.add(Permission.objects.get(codename='view_complaint'))
        self.client.force_login(user)
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(self.client.get('/merchants/').status_code, 403)
        self.assertEqual(self.client.post(f'/merchants/{self.merchant.pk}/complaints/{self.complaint.pk}/reply/', {}).status_code, 403)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(f'/merchants/{self.other.pk}/?complaint={self.complaint.pk}').status_code, 404)
        csrf = Client(enforce_csrf_checks=True); csrf.force_login(self.user)
        self.assertEqual(csrf.post(f'/merchants/{self.merchant.pk}/sync/', {}).status_code, 403)
        self.client.post(f'/merchants/{self.merchant.pk}/delete/')
        self.assertTrue(Merchant.objects.filter(pk=self.merchant.pk).exists())
        self.assertEqual(self.client.get(f'/merchants/{self.merchant.pk}/complaints/{self.complaint.pk}/image/?id=OTHER').status_code, 404)

    def test_complaint_detail_permission_and_merchant_boundary(self):
        url = f'/merchants/{self.merchant.pk}/complaints/{self.complaint.pk}/?dialog=1'
        self.assertEqual(self.client.get(url).status_code, 302)
        user = User.objects.create_user('without-view', password='TestOnly-12345')
        self.client.force_login(user)
        self.assertEqual(self.client.get(url).status_code, 403)
        user.user_permissions.add(Permission.objects.get(codename='view_complaint'))
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['X-Frame-Options'], 'SAMEORIGIN')
        self.assertEqual(self.client.post(url).status_code, 405)
        self.assertEqual(self.client.get(
            f'/merchants/{self.other.pk}/complaints/{self.complaint.pk}/?dialog=1').status_code, 404)

    def test_reply_refund_and_immediate_field_validation(self):
        other = Complaint.objects.create(merchant=self.other, complaint_id='OTHER')
        Media.objects.create(merchant=self.other, complaint=other, media_id='CROSS-MERCHANT')
        form = ReplyForm({'operation_id': str(uuid.uuid4()), 'content': '收到', 'media': '["CROSS-MERCHANT"]'}, complaint=self.complaint)
        self.assertFalse(form.is_valid())
        for data in [{'action': 'REJECT', 'acknowledged': True}, {'action': 'APPROVE', 'acknowledged': True}]:
            form = RefundForm({'operation_id': str(uuid.uuid4()), **data}, complaint=self.complaint)
            self.assertFalse(form.is_valid())
        form = RefundForm({'operation_id': str(uuid.uuid4()), 'action': 'APPROVE', 'launch_refund_day': 0, 'acknowledged': True}, complaint=self.complaint)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.payload(), {'action': 'APPROVE', 'launch_refund_day': 0})
        form = ImmediateForm({'operation_id': str(uuid.uuid4()), 'content': '人工回复'}, complaint=self.complaint)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.payload()['message']['blocks'][0], {'type': 'TEXT', 'text': {'text': '人工回复'}})

    def test_all_write_paths_and_notification_methods(self):
        seen = []
        def handle(request):
            seen.append((request.method, request.url.path))
            return self.response(None, 204) if request.method != 'GET' else self.response({'url': 'https://example.com'})
        gw = self.gw(handle)
        for kind in ('reply', 'complete', 'refund', 'immediate'): gw.action('C001', kind, {'test': '参数'})
        for method in ('GET', 'POST', 'PUT', 'DELETE'): gw.notification_config(method, 'https://example.com/callbacks/')
        self.assertEqual([url.split('/')[-1] for _, url in seen[:4]], ['response', 'complete', 'update-refund-progress', 'response-immediate-service'])
        self.assertEqual([method for method, _ in seen[4:]], ['GET', 'POST', 'PUT', 'DELETE'])

    def test_unknown_requires_fresh_sync_before_manual_resolution(self):
        op = Operation.objects.create(merchant=self.merchant, complaint=self.complaint, user=self.user,
            kind='reply', payload_hash='x', state='unknown')
        self.client.force_login(self.user)
        url = f'/operations/{op.pk}/resolve/'
        self.client.post(url, {'outcome': 'success', 'confirmed': 'on'})
        op.refresh_from_db(); self.assertEqual(op.state, 'unknown')
        self.complaint.save()
        self.client.post(url, {'outcome': 'success', 'confirmed': 'on'})
        op.refresh_from_db(); self.assertEqual(op.state, 'success')
