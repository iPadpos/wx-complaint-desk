import base64
import hashlib
import io
import json
import re
import secrets
import time
from pathlib import Path
from urllib.parse import quote

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from PIL import Image


class WechatError(Exception):
    def __init__(self, message, *, unknown=False, request_id='', code=''):
        super().__init__(message)
        self.unknown = unknown
        self.request_id = request_id[:128]
        self.code = code


def load_private(path):
    file = Path(path)
    if not file.is_absolute() or file.stat().st_mode & 0o007:
        raise ValueError('商户私钥须为绝对路径，且禁止其他用户读取')
    key = serialization.load_pem_private_key(file.read_bytes(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
        raise ValueError('商户私钥须为至少 2048 位 RSA 私钥')
    return key


def load_public(path):
    raw = Path(path).read_bytes()
    if b'BEGIN CERTIFICATE' in raw:
        key = x509.load_pem_x509_certificate(raw).public_key()
    else:
        key = serialization.load_pem_public_key(raw)
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
        raise ValueError('验签文件须为至少 2048 位 RSA 公钥或平台证书')
    return key


def credentials_for(merchant):
    if getattr(merchant, 'access_mode', 'direct') == 'channel':
        if not merchant.channel_id: raise WechatError('未选择微信渠道账号')
        return merchant.channel
    return merchant


def configuration_errors(merchant):
    if merchant.is_demo:
        return [] if settings.ALLOW_DEMO else ['此环境禁止演示商户']
    try: account = credentials_for(merchant)
    except WechatError as error: return [str(error)]
    from .models import WechatChannel
    if isinstance(account, WechatChannel) and not account.enabled:
        return ['微信渠道账号已停用']
    merchant = account
    errors = []
    for name, label in [('serial_no', '商户证书序列号'), ('private_key_path', '商户私钥'),
                        ('api_v3_key', 'APIv3 密钥'), ('public_key_id', '微信验签 ID'),
                        ('public_key_path', '微信验签公钥或证书')]:
        if not getattr(merchant, name): errors.append('缺少' + label)
    if len(merchant.api_v3_key.encode()) != 32: errors.append('APIv3 密钥须为32字节')
    try: load_private(merchant.private_key_path)
    except (ValueError, OSError, TypeError): errors.append('私钥路径、权限或格式不正确')
    try: load_public(merchant.public_key_path)
    except (ValueError, OSError, TypeError): errors.append('微信验签文件不可读或格式不正确')
    if bool(merchant.secondary_key_id) != bool(merchant.secondary_key_path):
        errors.append('备用验签 ID 和文件须同时配置')
    if merchant.secondary_key_path:
        try: load_public(merchant.secondary_key_path)
        except (ValueError, OSError, TypeError): errors.append('备用验签文件不可读或格式不正确')
    return errors


def verify_signature(merchant, headers, body):
    """微信响应及回调使用原始字节验签，ID 必须属于当前商户配置。"""
    headers = {k.lower(): v for k, v in headers.items()}
    try:
        timestamp = headers['wechatpay-timestamp']
        nonce = headers['wechatpay-nonce']
        signature = base64.b64decode(headers['wechatpay-signature'], validate=True)
        key_id = headers['wechatpay-serial']
        if abs(time.time() - int(timestamp)) > 300:
            raise ValueError('时间戳过期')
        keys = {merchant.public_key_id: merchant.public_key_path}
        if merchant.secondary_key_id: keys[merchant.secondary_key_id] = merchant.secondary_key_path
        if key_id not in keys: raise ValueError('未知验签 ID')
        load_public(keys[key_id]).verify(signature, timestamp.encode() + b'\n' + nonce.encode() + b'\n' + body + b'\n',
                                        padding.PKCS1v15(), hashes.SHA256())
    except Exception as error:
        raise WechatError('微信签名验证失败，请核对验签 ID、文件及系统时间') from error


def _decrypt_envelope(account, body):
    envelope = json.loads(body)
    resource = envelope['resource']
    if resource['algorithm'] != 'AEAD_AES_256_GCM': raise ValueError('不支持的算法')
    clear = AESGCM(account.api_v3_key.encode()).decrypt(
        resource['nonce'].encode(), base64.b64decode(resource['ciphertext'], validate=True),
        resource.get('associated_data', '').encode())
    event_id = envelope['id']
    if not isinstance(event_id, str) or not 1 <= len(event_id) <= 100: raise ValueError('通知ID无效')
    return envelope, event_id, json.loads(clear)


def decrypt_notification(merchant, body, *, check_merchant=True):
    try:
        _, event_id, data = _decrypt_envelope(merchant, body)
        if not isinstance(data.get('complaint_id'), str) or not 1 <= len(data['complaint_id']) <= 64:
            raise ValueError('投诉ID无效')
        if check_merchant and data.get('complainted_mchid', merchant.mch_id) != merchant.mch_id:
            raise ValueError('商户不匹配')
        return event_id, data
    except Exception as error:
        raise WechatError('微信通知解密失败或格式不正确') from error


VIOLATION_EVENTS = ('VIOLATION.PUNISH', 'VIOLATION.INTERCEPT', 'VIOLATION.APPEAL')


def decrypt_violation_notification(channel, body):
    """商户平台处置记录回调：返回通知 ID、事件类型和解密后的处置记录。"""
    try:
        envelope, event_id, data = _decrypt_envelope(channel, body)
        if envelope.get('event_type') not in VIOLATION_EVENTS: raise ValueError('事件类型无效')
        if not isinstance(data, dict): raise ValueError('处置记录格式无效')
        for key, limit in (('sub_mchid', 32), ('record_id', 128)):
            if not isinstance(data.get(key), str) or not 1 <= len(data[key]) <= limit: raise ValueError(key + '无效')
        return event_id, envelope['event_type'], data
    except Exception as error:
        raise WechatError('微信通知解密失败或格式不正确') from error


def valid_image(raw, max_bytes=2 * 1024 * 1024):
    if not raw or len(raw) > max_bytes: raise WechatError('图片大小不能超过2MB')
    try:
        with Image.open(io.BytesIO(raw)) as img:
            if img.format not in ('JPEG', 'PNG', 'BMP'): raise ValueError()
            if img.width * img.height > 40_000_000: raise ValueError()
            img.verify()
            return {'JPEG': 'image/jpeg', 'PNG': 'image/png', 'BMP': 'image/bmp'}[img.format]
    except Exception as error: raise WechatError('仅支持有效的 JPG、PNG、BMP 图片') from error


class WechatGateway:
    BASE = 'https://api.mch.weixin.qq.com'

    def __init__(self, merchant, transport=None):
        self.merchant = merchant
        errors = configuration_errors(merchant)
        if errors: raise WechatError('；'.join(errors))
        self.account = credentials_for(merchant)
        self.private = load_private(self.account.private_key_path)
        self.transport = transport
        self.request_id = ''

    def request(self, method, path, payload=None, params=None, image=None, binary=False):
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode() if payload is not None else b''
        kwargs = {'params': params, 'headers': {'Accept': '*/*' if binary else 'application/json',
                  'User-Agent': 'WechatComplaintDesk/1.0'}}
        if image:
            filename, raw, mime = image
            meta = json.dumps({'filename': filename, 'sha256': hashlib.sha256(raw).hexdigest()}, separators=(',', ':'))
            kwargs['files'] = {'meta': (None, meta, 'application/json'), 'file': (filename, raw, mime)}
            signing_body = meta.encode()
        else:
            kwargs['content'] = body
            kwargs['headers']['Content-Type'] = 'application/json'
            signing_body = body
        mutating = method != 'GET'
        with httpx.Client(timeout=15, transport=self.transport, follow_redirects=False, trust_env=False) as client:
            request = client.build_request(method, self.BASE + path, **kwargs)
            timestamp, nonce = str(int(time.time())), secrets.token_hex(16)
            canonical = (f'{method}\n{request.url.raw_path.decode()}\n{timestamp}\n{nonce}\n').encode() + signing_body + b'\n'
            signature = base64.b64encode(self.private.sign(canonical, padding.PKCS1v15(), hashes.SHA256())).decode()
            request.headers['Authorization'] = (
                f'WECHATPAY2-SHA256-RSA2048 mchid="{self.account.mch_id}",nonce_str="{nonce}",'
                f'timestamp="{timestamp}",serial_no="{self.account.serial_no}",signature="{signature}"')
            # 敏感字段解密及灰度能力要求此请求头。
            request.headers['Wechatpay-Serial'] = self.account.public_key_id
            try:
                response = client.send(request)
            except httpx.HTTPError as error:
                raise WechatError('微信请求连接失败或超时' + ('，结果未知，请先回查' if mutating else ''),
                                  unknown=mutating) from error
        self.request_id = response.headers.get('Request-ID', '')[:128]
        if response.status_code >= 400:
            try: code = str(response.json().get('code', 'API_ERROR'))
            except ValueError: code = 'API_ERROR'
            code = code if re.fullmatch(r'[A-Z0-9_]{1,64}', code) else 'API_ERROR'
            labels = {'NO_AUTH': '商户未获此接口权限或投诉归属不匹配', 'SIGN_ERROR': '请求签名错误',
                      'PARAM_ERROR': '参数错误，请核对表单及接口规则', 'RESOURCE_NOT_EXISTS': '资源不存在',
                      'FREQUENCY_LIMITED': '请求过于频繁，请稍后重试'}
            raise WechatError(f'微信接口 {code}：{labels.get(code, "请求未成功，请核对微信后台")}',
                              unknown=mutating and response.status_code >= 500, request_id=self.request_id, code=code)
        if not 200 <= response.status_code < 300:
            raise WechatError('微信返回非预期状态', unknown=mutating, request_id=self.request_id)
        # 微信图片接口的二进制下载不保证签名头；仅信任固定微信 HTTPS 源并校验图片字节。
        if not binary or response.headers.get('Wechatpay-Signature'):
            try: verify_signature(self.account, response.headers, response.content)
            except WechatError as error:
                raise WechatError(str(error), unknown=mutating, request_id=self.request_id) from error
        if binary: return response.content
        try: return response.json() if response.content else {}
        except ValueError as error:
            raise WechatError('微信应答格式错误', unknown=mutating, request_id=self.request_id) from error

    def list_complaints(self, begin, end, offset=0):
        return self.request('GET', '/v3/merchant-service/complaints-v2', params={
            'begin_date': begin, 'end_date': end, 'offset': offset, 'limit': 50,
            'complainted_mchid': self.merchant.mch_id})

    def complaint(self, cid):
        data = self.request('GET', '/v3/merchant-service/complaints-v2/' + quote(cid, safe=''))
        if data.get('payer_phone'):
            try:
                data['payer_phone'] = self.private.decrypt(base64.b64decode(data['payer_phone'], validate=True),
                    padding.OAEP(mgf=padding.MGF1(hashes.SHA1()), algorithm=hashes.SHA1(), label=None)).decode()
            except Exception as error: raise WechatError('投诉人手机号解密失败') from error
        return data

    def negotiations(self, cid, offset=0):
        return self.request('GET', f'/v3/merchant-service/complaints-v2/{quote(cid, safe="")}/negotiation-historys',
                            params={'limit': 300, 'offset': offset})

    def action(self, cid, kind, payload):
        suffix = {'reply': 'response', 'immediate': 'response-immediate-service',
                  'complete': 'complete', 'refund': 'update-refund-progress'}[kind]
        return self.request('POST', f'/v3/merchant-service/complaints-v2/{quote(cid, safe="")}/{suffix}', payload)

    def notification_config(self, method='GET', url=''):
        if self.account is not self.merchant:
            raise WechatError('渠道投诉通知请在渠道账号中统一配置')
        return self.request(method, '/v3/merchant-service/complaint-notifications',
                            {'url': url} if method in ('POST', 'PUT') else None)

    def violation_notification_config(self, method='GET', url=''):
        # 商户平台处置通知仅服务商（渠道账号）可配置，每个服务商只有一个地址。
        if self.account is not self.merchant or hasattr(self.merchant, 'access_mode'):
            raise WechatError('违规通知只能在渠道账号中配置')
        return self.request(method, '/v3/merchant-risk-manage/violation-notifications',
                            {'notify_url': url} if method in ('POST', 'PUT') else None)

    def limitations(self):
        # 查询渠道子商户的管控情况，由渠道账号代为调用。
        if self.account is self.merchant:
            raise WechatError('仅渠道代调用商户可查询管控情况')
        return self.request('GET', '/v3/mch-operation-manage/merchant-limitations/sub-mchid/'
                            + quote(self.merchant.mch_id, safe=''))

    def upload(self, filename, raw):
        mime = valid_image(raw)
        # 不发送客户端提供的路径、引号或非ASCII文件名。
        ext = {'image/jpeg': 'jpg', 'image/png': 'png', 'image/bmp': 'bmp'}[mime]
        data = self.request('POST', '/v3/merchant-service/images/upload', image=(f'evidence.{ext}', raw, mime))
        if not data.get('media_id'): raise WechatError('微信未返回媒体 ID', unknown=True)
        return data['media_id']

    def download(self, media_id):
        raw = self.request('GET', '/v3/merchant-service/images/' + quote(media_id, safe=''), binary=True)
        return raw, valid_image(raw, max_bytes=5 * 1024 * 1024)


def gateway_for(merchant):
    if merchant.is_demo:
        if not settings.ALLOW_DEMO: raise WechatError('当前环境禁止演示商户')
        from .demo import DemoGateway
        return DemoGateway(merchant)
    return WechatGateway(merchant)
