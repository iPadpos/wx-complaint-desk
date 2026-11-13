import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, connection
from django.test import TestCase, override_settings
from django.utils import timezone

from .credentials import save_merchant_configuration
from .forms import MerchantForm
from .models import Merchant, WechatChannel


class MerchantCredentialUploadTests(TestCase):
    mch_id = '1990023456'

    @classmethod
    def setUpTestData(cls):
        cls.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.private_pem = cls.private.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        cls.public_pem = cls.other.public_key().public_bytes(serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo)
        cls.cert_pem = cls.certificate(cls.mch_id)
        cls.serial = format(x509.load_pem_x509_certificate(cls.cert_pem).serial_number, 'X')
        cls.user = User.objects.create_superuser('merchant-upload', '', 'LocalOnly-merchant-upload-5481')

    @classmethod
    def certificate(cls, mch_id, *, expired=False):
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, mch_id)])
        return (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(cls.private.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(timezone.now() - timedelta(days=2))
            .not_valid_after(timezone.now() + timedelta(days=-1 if expired else 30))
            .sign(cls.private, hashes.SHA256()).public_bytes(serialization.Encoding.PEM))

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'credentials'
        override = override_settings(CHANNEL_CREDENTIAL_ROOT=self.root)
        override.enable()
        self.addCleanup(override.disable)
        self.client.force_login(self.user)

    def data(self, **changes):
        return {'name': '上传测试商户', 'mch_id': self.mch_id, 'access_mode': 'direct',
                'api_v3_key_input': 'm' * 32, 'public_key_id': 'PUB_KEY_ID_MERCHANT_TEST', **changes}

    def files(self):
        return {'certificate_upload': SimpleUploadedFile('apiclient_cert.pem', self.cert_pem),
                'private_key_upload': SimpleUploadedFile('apiclient_key.pem', self.private_pem),
                'public_key_upload': SimpleUploadedFile('pub_key.pem', self.public_pem)}

    def create_merchant(self):
        self.assertEqual(self.client.post('/merchants/new/', {**self.data(), **self.files()}).status_code, 302)
        return Merchant.objects.get(mch_id=self.mch_id)

    def edit_data(self, merchant, **changes):
        return {**{name: getattr(merchant, name) for name in MerchantForm.Meta.fields if name != 'api_v3_key_input'},
                'channel': merchant.channel_id or '', 'sort_order': merchant.sort_order or '', 'api_v3_key_input': '', **changes}

    def test_merchant_multipart_save_uses_restricted_storage_and_encrypted_key(self):
        with patch('httpx.Client.send') as send:
            merchant = self.create_merchant()
            send.assert_not_called()
        self.assertEqual(merchant.serial_no, self.serial)
        self.assertEqual(merchant.api_v3_key, 'm' * 32)
        self.assertEqual(merchant.access_mode, 'direct')
        for filename in (merchant.private_key_path, merchant.public_key_path):
            path = Path(filename)
            self.assertTrue(path.is_relative_to(self.root))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        with connection.cursor() as cursor:
            cursor.execute('SELECT api_v3_key FROM desk_merchant WHERE id=%s', [merchant.pk])
            self.assertNotIn('m' * 32, cursor.fetchone()[0])

    def test_blank_edit_preserves_saved_key_files_and_existing_submission_semantics(self):
        merchant = self.create_merchant()
        original_paths = (merchant.private_key_path, merchant.public_key_path)
        response = self.client.get(f'/merchants/{merchant.pk}/edit/')
        self.assertContains(response, 'data-api-key-status>已保存')
        self.assertNotContains(response, 'm' * 32)
        with patch('desk.credentials.tempfile.mkdtemp') as create_directory, patch('httpx.Client.send') as send:
            response = self.client.post(f'/merchants/{merchant.pk}/edit/', self.edit_data(merchant))
            self.assertEqual(response.status_code, 302)
            create_directory.assert_not_called()
            send.assert_not_called()
        merchant.refresh_from_db()
        self.assertEqual(merchant.api_v3_key, 'm' * 32)
        self.assertEqual((merchant.private_key_path, merchant.public_key_path), original_paths)

    def test_key_only_upload_rejects_mismatch_with_saved_merchant_certificate(self):
        merchant = self.create_merchant()
        other_key = self.other.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        form = MerchantForm(self.edit_data(merchant), {
            'private_key_upload': SimpleUploadedFile('apiclient_key.pem', other_key)}, instance=merchant)
        self.assertFalse(form.is_valid())
        self.assertIn('证书与私钥不配套', str(form.errors['private_key_upload']))
        self.assertEqual(len(list(self.root.iterdir())), 1)

    def test_key_only_upload_accepts_matching_saved_merchant_certificate(self):
        merchant = self.create_merchant()
        for _ in range(2):
            old_path = merchant.private_key_path
            form = MerchantForm(self.edit_data(merchant), {
                'private_key_upload': SimpleUploadedFile('apiclient_key.pem', self.private_pem)}, instance=merchant)
            self.assertTrue(form.is_valid(), form.errors)
            merchant = save_merchant_configuration(form)
            self.assertNotEqual(merchant.private_key_path, old_path)
            self.assertEqual(Path(merchant.private_key_path).read_bytes(), self.private_pem)
            self.assertEqual(Path(merchant.private_key_path).with_name('apiclient_cert.pem').read_bytes(), self.cert_pem)

    def test_key_only_upload_requires_merchant_certificate_when_saved_copy_unavailable(self):
        merchant = self.create_merchant()
        Path(merchant.private_key_path).with_name('apiclient_cert.pem').unlink()
        form = MerchantForm(self.edit_data(merchant), {
            'private_key_upload': SimpleUploadedFile('apiclient_key.pem', self.private_pem)}, instance=merchant)
        self.assertFalse(form.is_valid())
        self.assertIn('请同时上传配套的 apiclient_cert.pem', str(form.errors['certificate_upload']))

    def test_individual_private_upload_requires_serial_and_supports_public_upload(self):
        for serial, valid in [('', False), (self.serial, True)]:
            files = {'private_key_upload': SimpleUploadedFile('apiclient_key.pem', self.private_pem),
                     'public_key_upload': SimpleUploadedFile('pub_key.pem', self.public_pem)}
            form = MerchantForm(self.data(serial_no=serial), files)
            self.assertEqual(form.is_valid(), valid, form.errors)
            if valid:
                merchant = save_merchant_configuration(form)
                self.assertEqual(Path(merchant.private_key_path).read_bytes(), self.private_pem)
                self.assertEqual(merchant.serial_no, self.serial)
                self.assertFalse(Path(merchant.private_key_path).with_name('apiclient_cert.pem').exists())

    def test_key_only_upload_accepts_merchant_without_saved_key_with_manual_serial(self):
        merchant = Merchant.objects.create(name='未配置私钥', mch_id=self.mch_id)
        form = MerchantForm(self.edit_data(merchant, serial_no=self.serial), {
            'private_key_upload': SimpleUploadedFile('apiclient_key.pem', self.private_pem)}, instance=merchant)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.credential_uploads['apiclient_key.pem'], self.private_pem)
        self.assertNotIn('apiclient_cert.pem', form.credential_uploads)

    def test_invalid_account_expired_certificate_and_mismatched_private_save_nothing(self):
        other_private = self.other.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        cases = [(self.certificate('1990099999'), self.private_pem, '微信商户号不一致'),
                 (self.certificate(self.mch_id, expired=True), self.private_pem, '已过期'),
                 (self.cert_pem, other_private, '证书与私钥不配套')]
        for certificate, private, error in cases:
            with self.subTest(error=error):
                files = self.files()
                files['certificate_upload'] = SimpleUploadedFile('apiclient_cert.pem', certificate)
                files['private_key_upload'] = SimpleUploadedFile('apiclient_key.pem', private)
                response = self.client.post('/merchants/new/', {**self.data(), **files})
                self.assertContains(response, error)
                self.assertFalse(Merchant.objects.exists())
                self.assertFalse(self.root.exists())

    def test_platform_and_secondary_certificate_serials_are_saved(self):
        files = self.files()
        files['public_key_upload'] = SimpleUploadedFile('wechatpay.pem', self.cert_pem)
        files['secondary_key_upload'] = SimpleUploadedFile('secondary.pem', self.cert_pem)
        response = self.client.post('/merchants/new/', {**self.data(public_key_id=''), **files})
        self.assertEqual(response.status_code, 302)
        merchant = Merchant.objects.get()
        self.assertEqual(merchant.public_key_id, self.serial)
        self.assertEqual(merchant.secondary_key_id, self.serial)
        self.assertTrue(Path(merchant.secondary_key_path).is_file())

    def test_channel_mode_ignores_uploaded_merchant_credentials(self):
        channel = WechatChannel.objects.create(name='测试渠道', mch_id='1990090001')
        files = {name: SimpleUploadedFile('invalid.pem', b'not a valid pem') for name in self.files()}
        with patch('desk.forms.parse_merchant_certificate') as parse, patch('desk.credentials.tempfile.mkdtemp') as store:
            response = self.client.post('/merchants/new/', {**self.data(access_mode='channel', channel=channel.pk), **files})
            self.assertEqual(response.status_code, 302)
            parse.assert_not_called()
            store.assert_not_called()
        merchant = Merchant.objects.get()
        self.assertEqual(merchant.channel_id, channel.pk)
        self.assertEqual(merchant.api_v3_key, '')
        self.assertEqual(merchant.private_key_path, '')

    def test_legacy_path_only_post_has_no_new_file_parsing_or_storage(self):
        data = self.data(serial_no='LEGACY_SERIAL', private_key_path='/legacy/private.pem',
                         public_key_path='/legacy/public.pem')
        data.pop('access_mode')
        with patch('desk.forms.clean_credential_uploads') as parse, patch('desk.credentials.tempfile.mkdtemp') as store:
            response = self.client.post('/merchants/new/', data)
            self.assertEqual(response.status_code, 302)
            parse.assert_not_called()
            store.assert_not_called()
        self.assertEqual(Merchant.objects.get().private_key_path, '/legacy/private.pem')

    def test_database_failure_cleans_new_materials_and_preserves_saved_configuration(self):
        merchant = self.create_merchant()
        old_path = merchant.private_key_path
        form = MerchantForm(self.edit_data(merchant), self.files(), instance=merchant)
        self.assertTrue(form.is_valid(), form.errors)
        with patch.object(Merchant, 'save', side_effect=DatabaseError('controlled failure')):
            with self.assertRaises(DatabaseError): save_merchant_configuration(form)
        merchant.refresh_from_db()
        self.assertEqual(merchant.private_key_path, old_path)
        self.assertTrue(Path(old_path).is_file())
        self.assertEqual(len(list(self.root.iterdir())), 1)
