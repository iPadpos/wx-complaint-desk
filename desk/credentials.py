"""渠道凭据上传：只解析所需材料，私密文件保存在站点目录之外。"""
import io
import os
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone


MAX_PEM_SIZE = 64 * 1024
MAX_ZIP_SIZE = 2 * 1024 * 1024


class CredentialStorageError(Exception):
    pass


def saved_credential_files(account):
    """只展示已持久保存的配置，不读取凭据内容或把本次输入标成已保存。"""
    if not account.pk:
        return {}
    saved = {}
    for upload, attribute in [('private_key_upload', 'private_key_path'),
                              ('public_key_upload', 'public_key_path'),
                              ('secondary_key_upload', 'secondary_key_path')]:
        path = getattr(account, attribute, '')
        if path:
            saved[upload] = {'name': Path(path).name,
                             'caption': '文件配置已保存；不选择新文件则保留'}
    if account.serial_no:
        saved['certificate_upload'] = {'name': 'API 证书信息已保存',
                                       'caption': '证书序列号已保存；不选择新文件则保留'}
    return saved


def read_upload(upload, maximum):
    if upload.size > maximum:
        raise ValidationError(f'文件不能超过 {maximum // 1024} KB')
    raw = upload.read(maximum + 1)
    if not raw or len(raw) > maximum:
        raise ValidationError('文件为空或超过大小限制')
    return raw


def parse_private(raw):
    try:
        key = serialization.load_pem_private_key(raw, password=None)
        if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
            raise ValueError()
        return key
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise ValidationError('请上传有效的未加密 RSA 私钥 PEM，至少 2048 位')


def parse_verification(raw):
    try:
        certificate = x509.load_pem_x509_certificate(raw) if b'BEGIN CERTIFICATE' in raw else None
        key = certificate.public_key() if certificate else serialization.load_pem_public_key(raw)
        if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
            raise ValueError()
        return format(certificate.serial_number, 'X') if certificate else None
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise ValidationError('请上传微信支付 RSA 公钥 PEM 或平台证书，至少 2048 位；不能上传私钥')


def parse_merchant_certificate(raw, mch_id, *, account_label='微信渠道号'):
    try:
        certificate = x509.load_pem_x509_certificate(raw)
        public = certificate.public_key()
        if not isinstance(public, rsa.RSAPublicKey) or public.key_size < 2048:
            raise ValueError()
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise ValidationError('请上传有效的商户 API 证书 apiclient_cert.pem')
    names = [item.value for item in certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)]
    if mch_id not in names:
        raise ValidationError(f'证书所属微信账号与所填{account_label}不一致')
    if not certificate.not_valid_before_utc <= timezone.now() <= certificate.not_valid_after_utc:
        raise ValidationError('证书尚未生效或已过期')
    return certificate, format(certificate.serial_number, 'X')


def parse_certificate_bundle(upload, mch_id):
    raw = read_upload(upload, MAX_ZIP_SIZE)
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) > 20 or sum(item.file_size for item in entries) > 256 * 1024:
                raise ValidationError('证书包内容过多或解压后体积超过限制')
            selected = {}
            for item in entries:
                path = PurePosixPath(item.filename)
                if path.is_absolute() or '..' in path.parts or '\\' in item.filename or ':' in item.filename:
                    raise ValidationError('证书包包含不允许的文件路径')
                if item.flag_bits & 1:
                    raise ValidationError('请上传未加密的微信 API 证书 ZIP')
                if path.name in ('apiclient_cert.pem', 'apiclient_key.pem') and not item.is_dir():
                    if path.name in selected or item.file_size > MAX_PEM_SIZE:
                        raise ValidationError('证书包包含重复材料或材料过大')
                    selected[path.name] = archive.read(item)
        if len(selected) != 2:
            raise ValidationError('证书包须同时包含 apiclient_cert.pem 和 apiclient_key.pem')
        certificate, serial = parse_merchant_certificate(selected['apiclient_cert.pem'], mch_id)
        private = parse_private(selected['apiclient_key.pem'])
        if certificate.public_key().public_numbers() != private.public_key().public_numbers():
            raise ValidationError('证书与私钥不配套，请上传同一账号的一套材料')
        return selected, serial
    except (zipfile.BadZipFile, RuntimeError, OSError, ValueError, TypeError, UnsupportedAlgorithm):
        raise ValidationError('证书 ZIP 无法解析，请上传完整的微信 API 证书包')


def save_channel_configuration(form):
    return save_credential_configuration(form)


def save_merchant_configuration(form):
    if form.cleaned_data.get('access_mode') == 'channel':
        return form.save()
    return save_credential_configuration(form)


def save_credential_configuration(form):
    uploads = form.credential_uploads
    if not uploads:
        return form.save()
    root = Path(settings.CHANNEL_CREDENTIAL_ROOT).expanduser().absolute()
    forbidden = [Path(settings.BASE_DIR), Path(settings.STATIC_ROOT),
                 *[Path(path) for path in settings.STATICFILES_DIRS]]
    if any(root.resolve().is_relative_to(path.resolve()) for path in forbidden):
        raise CredentialStorageError('凭据上传目录须位于站点源码和静态目录之外，请联系管理员配置')
    directory = None
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix=form.cleaned_data['mch_id'] + '-', dir=root))
        for filename, raw in uploads.items():
            fd = os.open(directory / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as handle:
                handle.write(raw)
        account = form.save(commit=False)
        for filename, field in [('apiclient_key.pem', 'private_key_path'), ('wechatpay_verification.pem', 'public_key_path'),
                                ('wechatpay_secondary.pem', 'secondary_key_path')]:
            if filename in uploads:
                setattr(account, field, str(directory / filename))
        with transaction.atomic():
            account.save()
            form.save_m2m()
        return account
    except Exception as error:
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)
        if isinstance(error, OSError):
            raise CredentialStorageError('凭据文件保存失败，请确认上传目录可写后重新上传') from error
        raise
