import io
import os
import tempfile
import zipfile
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

from .credentials import CredentialStorageError, save_channel_configuration
from .forms import ChannelForm
from .models import WechatChannel
from .wechat import load_private, load_public


class CredentialUploadTests(TestCase):
    mch_id = '1990012345'
    public_key_id = 'PUB_KEY_ID_UPLOAD_TEST'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.private_pem = cls.private.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        cls.public_pem = cls.public_private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        cls.cert_pem = cls.certificate(cls.mch_id).public_bytes(serialization.Encoding.PEM)
        cls.serial = format(x509.load_pem_x509_certificate(cls.cert_pem).serial_number, 'X')

    @classmethod
    def certificate(cls, mch_id, *, expired=False, private=None):
        private = private or cls.private
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, mch_id)])
        return (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(private.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(timezone.now() - timedelta(days=2))
            .not_valid_after(timezone.now() + timedelta(days=-1 if expired else 30))
            .sign(private, hashes.SHA256()))

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'private-uploads'
        override = override_settings(CHANNEL_CREDENTIAL_ROOT=self.root)
        override.enable()
        self.addCleanup(override.disable)
        self.user = User.objects.create_superuser('upload-admin', '', 'TestOnly-credential-1831')
        self.client.force_login(self.user)

    def data(self, **overrides):
        return {'name': '上传测试渠道', 'mch_id': self.mch_id, 'provider': '测试机构',
                'api_v3_key_input': 'u' * 32, 'public_key_id': self.public_key_id, **overrides}

    def bundle(self, **overrides):
        return self.make_zip({'apiclient_cert.pem': self.cert_pem, 'apiclient_key.pem': self.private_pem, **overrides})

    def make_zip(self, contents):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, raw in contents.items(): archive.writestr(name, raw)
        return SimpleUploadedFile('certificates.zip', output.getvalue(), content_type='application/zip')

    def files(self):
        return {'certificate_upload': SimpleUploadedFile('apiclient_cert.pem', self.cert_pem),
                'private_key_upload': SimpleUploadedFile('apiclient_key.pem', self.private_pem),
                'public_key_upload': SimpleUploadedFile('public.pem', self.public_pem)}

    def create_channel(self):
        form = ChannelForm(self.data(), self.files())
        self.assertTrue(form.is_valid(), form.errors)
        return save_channel_configuration(form)

    def edit_data(self, channel, **overrides):
        data = {name: getattr(channel, name) for name in ChannelForm.Meta.fields if name != 'api_v3_key_input'}
        return {**data, 'api_v3_key_input': '', **overrides}

    def assert_no_uploads(self):
        self.assertFalse(self.root.exists() and list(self.root.iterdir()))

    def test_multipart_save_imports_serial_key_and_public_file_without_network(self):
        data = {**self.data(serial_no='PUB_KEY_ID_WRONG_POSITION'), **self.files()}
        with patch('httpx.Client.send') as send:
            response = self.client.post('/channels/new/', data)
            send.assert_not_called()
        self.assertEqual(response.status_code, 302)
        channel = WechatChannel.objects.get()
        self.assertEqual(channel.serial_no, self.serial)
        self.assertEqual(channel.api_v3_key, 'u' * 32)
        self.assertFalse(channel.enabled)
        private = Path(channel.private_key_path)
        public = Path(channel.public_key_path)
        self.assertEqual(private.stat().st_mode & 0o777, 0o600)
        self.assertEqual(private.parent.stat().st_mode & 0o777, 0o700)
        self.assertTrue(private.is_relative_to(self.root))
        self.assertTrue(public.is_relative_to(self.root))
        self.assertEqual(load_private(str(private)).public_key().public_numbers(), self.private.public_key().public_numbers())
        self.assertEqual(load_public(str(public)).public_numbers(), self.public_private.public_key().public_numbers())
        self.assertTrue((private.parent / 'apiclient_cert.pem').exists())

    def test_saved_status_blank_edit_and_encrypted_storage(self):
        channel = self.create_channel()
        original_paths = (channel.private_key_path, channel.public_key_path)
        with connection.cursor() as cursor:
            cursor.execute('SELECT api_v3_key FROM desk_wechatchannel WHERE id=%s', [channel.pk])
            self.assertNotIn('u' * 32, cursor.fetchone()[0])
        response = self.client.get(f'/channels/{channel.pk}/edit/')
        self.assertContains(response, '当前密钥已保存，不回显')
        self.assertContains(response, 'data-api-key-status>已保存')
        self.assertNotContains(response, 'u' * 32)
        self.assertEqual(self.client.post(f'/channels/{channel.pk}/edit/', self.edit_data(channel)).status_code, 302)
        channel.refresh_from_db()
        self.assertEqual(channel.api_v3_key, 'u' * 32)
        self.assertEqual((channel.private_key_path, channel.public_key_path), original_paths)
        self.assertEqual(len(list(self.root.iterdir())), 1)

    def test_replace_key_and_validation_error_do_not_clear_saved_key(self):
        channel = self.create_channel()
        url = f'/channels/{channel.pk}/edit/'
        self.client.post(url, self.edit_data(channel, api_v3_key_input='v' * 32))
        channel.refresh_from_db()
        self.assertEqual(channel.api_v3_key, 'v' * 32)
        response = self.client.post(url, self.edit_data(channel, api_v3_key_input='bad', serial_no='PUB_KEY_ID_WRONG'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '保存未成功')
        self.assertContains(response, 'APIv3 密钥必须为32字节')
        self.assertContains(response, '当前密钥已保存')
        channel.refresh_from_db()
        self.assertEqual(channel.api_v3_key, 'v' * 32)
        self.assertEqual(channel.serial_no, self.serial)

    def test_zip_must_match_account_and_private_key(self):
        wrong_certificate = self.certificate('1990099999').public_bytes(serialization.Encoding.PEM)
        other_key = self.other_private.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        for contents, expected in [
            ({'apiclient_cert.pem': wrong_certificate, 'apiclient_key.pem': self.private_pem}, '微信渠道号不一致'),
            ({'apiclient_cert.pem': self.cert_pem, 'apiclient_key.pem': other_key}, '证书与私钥不配套')]:
            form = ChannelForm(self.data(), {'certificate_bundle': self.make_zip(contents)})
            self.assertFalse(form.is_valid())
            self.assertIn(expected, str(form.errors))
        self.assert_no_uploads()

    def test_individual_certificate_must_match_channel_and_uploaded_private_key(self):
        wrong_certificate = self.certificate('1990099999').public_bytes(serialization.Encoding.PEM)
        other_key = self.other_private.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        for certificate, private, field in [(wrong_certificate, self.private_pem, 'certificate_upload'),
                                            (self.cert_pem, other_key, 'private_key_upload')]:
            form = ChannelForm(self.data(), {
                'certificate_upload': SimpleUploadedFile('apiclient_cert.pem', certificate),
                'private_key_upload': SimpleUploadedFile('apiclient_key.pem', private)})
            self.assertFalse(form.is_valid())
            self.assertIn(field, form.errors)
        self.assert_no_uploads()

    def test_individual_certificate_requires_private_key_for_new_channel(self):
        form = ChannelForm(self.data(), {'certificate_upload': SimpleUploadedFile('cert.pem', self.cert_pem)})
        self.assertFalse(form.is_valid())
        self.assertIn('private_key_upload', form.errors)
        self.assert_no_uploads()

    def test_individual_certificate_edit_can_reuse_matching_existing_private_key(self):
        channel = self.create_channel()
        old_path = channel.private_key_path
        form = ChannelForm(self.edit_data(channel), {
            'certificate_upload': SimpleUploadedFile('cert.pem', self.cert_pem)}, instance=channel)
        self.assertTrue(form.is_valid(), form.errors)
        channel = save_channel_configuration(form)
        self.assertEqual(channel.private_key_path, old_path)
        self.assertEqual(channel.serial_no, self.serial)

    def test_key_only_upload_rejects_mismatch_with_saved_channel_certificate(self):
        channel = self.create_channel()
        other_key = self.other_private.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        form = ChannelForm(self.edit_data(channel), {
            'private_key_upload': SimpleUploadedFile('apiclient_key.pem', other_key)}, instance=channel)
        self.assertFalse(form.is_valid())
        self.assertIn('证书与私钥不配套', str(form.errors['private_key_upload']))
        self.assertEqual(len(list(self.root.iterdir())), 1)

    def test_key_only_upload_accepts_matching_saved_channel_certificate(self):
        channel = self.create_channel()
        for _ in range(2):
            old_path = channel.private_key_path
            form = ChannelForm(self.edit_data(channel), {
                'private_key_upload': SimpleUploadedFile('apiclient_key.pem', self.private_pem)}, instance=channel)
            self.assertTrue(form.is_valid(), form.errors)
            channel = save_channel_configuration(form)
            self.assertNotEqual(channel.private_key_path, old_path)
            self.assertEqual(Path(channel.private_key_path).read_bytes(), self.private_pem)
            self.assertEqual(Path(channel.private_key_path).with_name('apiclient_cert.pem').read_bytes(), self.cert_pem)

    def test_key_only_upload_requires_channel_certificate_when_saved_copy_unavailable(self):
        channel = self.create_channel()
        Path(channel.private_key_path).with_name('apiclient_cert.pem').unlink()
        form = ChannelForm(self.edit_data(channel), {
            'private_key_upload': SimpleUploadedFile('apiclient_key.pem', self.private_pem)}, instance=channel)
        self.assertFalse(form.is_valid())
        self.assertIn('请同时上传配套的 apiclient_cert.pem', str(form.errors['certificate_upload']))

    def test_certificate_file_widget_accepts_pem_and_zip_is_not_rendered_on_page(self):
        response = self.client.get('/channels/new/')
        self.assertContains(response, 'name="certificate_upload"')
        self.assertNotContains(response, 'name="certificate_bundle"')
        self.assertContains(response, 'apiclient_cert.pem')

    def test_expired_and_incomplete_certificate_bundles_rejected(self):
        expired = self.certificate(self.mch_id, expired=True).public_bytes(serialization.Encoding.PEM)
        for contents in [{'apiclient_cert.pem': expired, 'apiclient_key.pem': self.private_pem},
                         {'apiclient_cert.pem': self.cert_pem}]:
            form = ChannelForm(self.data(), {'certificate_bundle': self.make_zip(contents)})
            self.assertFalse(form.is_valid())
            self.assertIn('certificate_bundle', form.errors)
        self.assert_no_uploads()

    def test_path_traversal_zip_bomb_and_malformed_zip_rejected_before_storage(self):
        uploads = [self.bundle(**{'../outside.pem': b'bad'}), self.bundle(**{'oversize.txt': b'a' * (300 * 1024)}),
                   SimpleUploadedFile('bad.zip', b'not a ZIP')]
        for upload in uploads:
            response = self.client.post('/channels/new/', {**self.data(), 'certificate_bundle': upload})
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, '保存未成功')
        self.assert_no_uploads()
        self.assertFalse(WechatChannel.objects.exists())

    def test_private_key_cannot_be_uploaded_as_verification_public_key(self):
        form = ChannelForm(self.data(), {'public_key_upload': SimpleUploadedFile('bad.pem', self.private_pem)})
        self.assertFalse(form.is_valid())
        self.assertIn('public_key_upload', form.errors)
        self.assertNotIn(self.private_pem.decode(), str(form.errors))
        self.assert_no_uploads()

    def test_individual_private_upload_requires_serial_and_supports_public_upload(self):
        for serial, valid in [('', False), (self.serial, True)]:
            files = {'private_key_upload': SimpleUploadedFile('private.pem', self.private_pem),
                     'public_key_upload': SimpleUploadedFile('public.pem', self.public_pem)}
            if valid: files['certificate_upload'] = SimpleUploadedFile('apiclient_cert.pem', self.cert_pem)
            form = ChannelForm(self.data(serial_no=serial), files)
            self.assertEqual(form.is_valid(), valid, form.errors)
            if valid:
                channel = save_channel_configuration(form)
                self.assertTrue(Path(channel.private_key_path).is_file())
                self.assertEqual(channel.serial_no, self.serial)

    def test_public_key_id_and_certificate_serial_cannot_be_confused(self):
        form = ChannelForm(self.data(serial_no='PUB_KEY_ID_WRONG_FIELD'))
        self.assertFalse(form.is_valid())
        self.assertIn('serial_no', form.errors)
        form = ChannelForm(self.data(public_key_id=''), {'public_key_upload': SimpleUploadedFile('public.pem', self.public_pem)})
        self.assertFalse(form.is_valid())
        self.assertIn('public_key_id', form.errors)

    def test_platform_certificate_verification_serial_is_imported(self):
        certificate = self.certificate('WeChatPay platform', private=self.public_private)
        form = ChannelForm(self.data(public_key_id=''), {'public_key_upload': SimpleUploadedFile(
            'platform.pem', certificate.public_bytes(serialization.Encoding.PEM))})
        self.assertTrue(form.is_valid(), form.errors)
        channel = save_channel_configuration(form)
        self.assertEqual(channel.public_key_id, format(certificate.serial_number, 'X'))
        self.assertEqual(load_public(channel.public_key_path).public_numbers(), self.public_private.public_key().public_numbers())

    def test_database_failure_removes_new_files_and_keeps_existing_configuration(self):
        channel = self.create_channel()
        old_path = channel.private_key_path
        form = ChannelForm(self.edit_data(channel), self.files(), instance=channel)
        self.assertTrue(form.is_valid(), form.errors)
        with patch.object(WechatChannel, 'save', side_effect=DatabaseError('controlled failure')):
            with self.assertRaises(DatabaseError): save_channel_configuration(form)
        channel.refresh_from_db()
        self.assertEqual(channel.private_key_path, old_path)
        self.assertTrue(Path(old_path).exists())
        self.assertEqual(len(list(self.root.iterdir())), 1)

    def test_file_write_failure_saves_no_channel_or_partial_materials(self):
        form = ChannelForm(self.data(), self.files())
        self.assertTrue(form.is_valid(), form.errors)
        original_open = os.open
        def fail_writes(path, flags, *args, **kwargs):
            if flags & os.O_WRONLY: raise PermissionError('controlled failure')
            return original_open(path, flags, *args, **kwargs)
        with patch('desk.credentials.os.open', side_effect=fail_writes):
            with self.assertRaises(CredentialStorageError): save_channel_configuration(form)
        self.assertFalse(WechatChannel.objects.exists())
        self.assert_no_uploads()

    def test_static_or_source_storage_root_is_rejected(self):
        form = ChannelForm(self.data(), self.files())
        self.assertTrue(form.is_valid(), form.errors)
        from django.conf import settings
        with override_settings(CHANNEL_CREDENTIAL_ROOT=settings.STATIC_ROOT / 'uploaded-keys'):
            with self.assertRaises(CredentialStorageError): save_channel_configuration(form)
        self.assertFalse(WechatChannel.objects.exists())

    def test_replacing_files_creates_new_version_without_overwriting_old_files(self):
        channel = self.create_channel()
        old_path = channel.private_key_path
        old_bytes = Path(old_path).read_bytes()
        form = ChannelForm(self.edit_data(channel), self.files(), instance=channel)
        self.assertTrue(form.is_valid(), form.errors)
        channel = save_channel_configuration(form)
        self.assertNotEqual(channel.private_key_path, old_path)
        self.assertEqual(Path(old_path).read_bytes(), old_bytes)
        self.assertTrue(Path(channel.private_key_path).is_file())
