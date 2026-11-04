import re
import uuid
from urllib.parse import urlsplit
from django import forms
from django.db.models import Q
from django.utils import timezone
from .credentials import parse_certificate_bundle, parse_merchant_certificate, parse_private, parse_verification, read_upload, saved_credential_files, MAX_PEM_SIZE
from .models import Merchant, WechatChannel
from .services import allowed_media
from .wechat import load_private


def clean_credential_uploads(form, data):
    form.credential_uploads = {}
    bundle, private_upload = data.get('certificate_bundle'), data.get('private_key_upload')
    certificate_upload = data.get('certificate_upload')
    if bundle and (private_upload or certificate_upload):
        form.add_error(None, '请分别上传证书和私钥，或使用原有证书包导入，不能混用')
    elif bundle and data.get('mch_id'):
        try:
            files, serial = parse_certificate_bundle(bundle, data['mch_id'])
            form.credential_uploads.update(files)
            data['serial_no'] = serial
        except forms.ValidationError as error: form.add_error('certificate_bundle', error)
    else:
        certificate, private = None, None
        if certificate_upload and data.get('mch_id'):
            try:
                raw = read_upload(certificate_upload, MAX_PEM_SIZE)
                certificate, serial = parse_merchant_certificate(raw, data['mch_id'], account_label=form.fields['mch_id'].label)
                form.credential_uploads['apiclient_cert.pem'] = raw
                data['serial_no'] = serial
            except forms.ValidationError as error: form.add_error('certificate_upload', error)
        if private_upload:
            try:
                raw = read_upload(private_upload, MAX_PEM_SIZE)
                private = parse_private(raw)
                form.credential_uploads['apiclient_key.pem'] = raw
                if not data.get('serial_no'):
                    form.add_error('serial_no', '请同时上传 apiclient_cert.pem 自动读取，或填写配套的 API 证书序列号')
            except forms.ValidationError as error: form.add_error('private_key_upload', error)
        if certificate is not None:
            if private is None and not private_upload:
                path = data.get('private_key_path')
                try:
                    if path: private = load_private(path)
                except (OSError, ValueError, TypeError):
                    form.add_error('private_key_upload', '现有私钥不可用，请同时上传配套的 apiclient_key.pem')
                if not path:
                    form.add_error('private_key_upload', '请同时上传配套的 apiclient_key.pem')
            if private is not None and certificate.public_key().public_numbers() != private.public_key().public_numbers():
                form.add_error('private_key_upload', '证书与私钥不配套，请上传同一账号的一套材料')
    if data.get('serial_no', '').startswith('PUB_KEY_ID_'):
        form.add_error('serial_no', '这里需要 API 证书序列号；PUB_KEY_ID 请填写在微信公钥 ID 中')
    for upload_name, id_name, filename in [('public_key_upload', 'public_key_id', 'wechatpay_verification.pem'),
                                          ('secondary_key_upload', 'secondary_key_id', 'wechatpay_secondary.pem')]:
        upload = data.get(upload_name)
        if not upload: continue
        try:
            raw = read_upload(upload, MAX_PEM_SIZE)
            serial = parse_verification(raw)
            key_id = data.get(id_name, '')
            if serial:
                if key_id and key_id.upper() != serial:
                    form.add_error(id_name, '验签 ID 与所上传平台证书的序列号不一致')
                else: data[id_name] = serial
            elif not key_id.startswith('PUB_KEY_ID_'):
                form.add_error(id_name, '请填写此微信支付公钥对应的 PUB_KEY_ID')
            form.credential_uploads[filename] = raw
        except forms.ValidationError as error: form.add_error(upload_name, error)
    if bool(data.get('secondary_key_id')) != bool(data.get('secondary_key_path') or data.get('secondary_key_upload')):
        form.add_error('secondary_key_path', '备用验签 ID 和文件须同时配置')


class MerchantForm(forms.ModelForm):
    api_v3_key_input = forms.CharField(label='APIv3 密钥', required=False,
        widget=forms.PasswordInput(render_value=False), help_text='32字节；编辑时留空保留')
    certificate_upload = forms.FileField(label='上传商户 API 证书 PEM', required=False,
        widget=forms.ClearableFileInput(attrs={'accept': '.pem', 'data-certificate-target': 'serial_no'}),
        help_text='选择 apiclient_cert.pem 后立即填入证书序列号，保存时再次校验')
    private_key_upload = forms.FileField(label='上传商户私钥 PEM', required=False,
        widget=forms.ClearableFileInput(attrs={'accept': '.pem'}),
        help_text='选择与证书配套的 apiclient_key.pem')
    public_key_upload = forms.FileField(label='上传微信公钥／平台证书 PEM', required=False,
        widget=forms.ClearableFileInput(attrs={'accept': '.pem', 'data-certificate-target': 'public_key_id',
                                             'data-allow-public-key': 'true'}),
        help_text='选择平台证书后立即填入序列号；pub_key.pem 不含公钥 ID，需另行填写 PUB_KEY_ID')
    secondary_key_upload = forms.FileField(label='上传备用验签文件 PEM', required=False,
        widget=forms.ClearableFileInput(attrs={'accept': '.pem', 'data-certificate-target': 'secondary_key_id',
                                             'data-allow-public-key': 'true'}))

    class Meta:
        model = Merchant
        fields = ['name', 'mch_id', 'access_mode', 'channel', 'provider_merchant_id', 'enabled',
                  'serial_no', 'private_key_path', 'api_v3_key_input',
                  'public_key_id', 'public_key_path', 'secondary_key_id', 'secondary_key_path',
                  'notify_url', 'allowed_jump_hosts']
        help_texts = {'private_key_path': '服务端绝对路径',
                      'public_key_path': 'PEM 文件',
                      'notify_url': '需在通知配置中注册'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.original_identity = (self.instance.access_mode, self.instance.channel_id)
        self.credential_uploads = {}
        self.saved_credential_files = saved_credential_files(self.instance)
        self.api_v3_configured = bool(self.instance.pk and self.instance.api_v3_key)
        self.fields['api_v3_key_input'].help_text = (
            '当前密钥已保存，不回显；留空保留，填写新值则替换' if self.api_v3_configured else
            '尚未保存密钥；请输入32字节 APIv3 密钥')
        self.fields['serial_no'].help_text = '选择 apiclient_cert.pem 后自动读取；也可手动填写，不能填写 PUB_KEY_ID'
        # 兼容原有商户表单提交；旧调用未传新字段时仍为自行调用。
        self.fields['access_mode'].required = False
        channels = WechatChannel.objects.filter(enabled=True)
        if self.instance.channel_id:
            channels = WechatChannel.objects.filter(Q(enabled=True) | Q(pk=self.instance.channel_id))
        self.fields['channel'].queryset = channels
        self.fields['channel'].empty_label = '请选择渠道账号'
        self.fields['provider_merchant_id'].help_text = '支付机构内部编号，例如随行付商户编号；可选'
        for name in ('private_key_path', 'public_key_path', 'secondary_key_path'):
            self.fields[name].widget.attrs['autocomplete'] = 'off'
        self.fields['api_v3_key_input'].widget.attrs['autocomplete'] = 'new-password'
        if self.is_channel_mode:
            self.fields['mch_id'].label = '微信子商户号'
            for field in self.credential_fields:
                self.fields[field.name].disabled = True
        else:
            # 自行调用时不展示渠道编号，也不因隐藏字段未提交而清除已有编号。
            self.fields['provider_merchant_id'].disabled = True

    @property
    def is_channel_mode(self):
        return (self.data.get('access_mode', 'direct') if self.is_bound else self.instance.access_mode) == 'channel'

    @property
    def basic_fields(self):
        return [self[name] for name in ('name', 'mch_id', 'access_mode', 'enabled')]

    @property
    def credential_fields(self):
        return [self[name] for name in ('serial_no', 'private_key_path', 'api_v3_key_input', 'public_key_id',
            'public_key_path', 'secondary_key_id', 'secondary_key_path', 'notify_url',
            'certificate_upload', 'private_key_upload', 'public_key_upload', 'secondary_key_upload')]

    @property
    def server_path_fields(self):
        return [self[name] for name in ('private_key_path', 'public_key_path', 'secondary_key_path')]

    @property
    def has_server_path_errors(self):
        return any(name in self.errors for name in ('private_key_path', 'public_key_path', 'secondary_key_path'))

    def clean_mch_id(self):
        mch = self.cleaned_data['mch_id']
        if not re.fullmatch(r'\d{6,32}', mch): raise forms.ValidationError('请输入6至32位数字商户号')
        if self.instance.pk and mch != self.instance.mch_id:
            raise forms.ValidationError('已保存商户号不能变更，请新增商户以保持投诉归属')
        return mch

    def clean(self):
        data = super().clean()
        mode = data.get('access_mode') or 'direct'
        data['access_mode'] = mode
        channel = data.get('channel')
        if mode == 'channel':
            if not channel: self.add_error('channel', '请选择用于代调用的微信渠道账号')
            if self.instance.is_demo: self.add_error('access_mode', '演示商户不能使用真实渠道账号')
        else:
            data['channel'] = None
        identity = (mode, data['channel'].pk if data.get('channel') else None)
        if self.instance.pk and identity != self.original_identity and (
            self.instance.complaints.exists() or self.instance.jobs.exists() or
            self.instance.operation_set.exists() or self.instance.notification_set.exists()):
            self.add_error('access_mode', '该商户已有业务记录，请保留接入方式和渠道归属，避免历史任务串用账号')
        key = data.get('api_v3_key_input', '')
        if key and len(key.encode()) != 32: self.add_error('api_v3_key_input', 'APIv3 密钥必须为32字节')
        if key: self.instance.api_v3_key = key
        has_uploads = any(data.get(name) for name in (
            'certificate_upload', 'private_key_upload', 'public_key_upload', 'secondary_key_upload'))
        if mode != 'channel' and has_uploads:
            clean_credential_uploads(self, data)
        elif bool(data.get('secondary_key_id')) != bool(data.get('secondary_key_path')):
            self.add_error('secondary_key_path', '备用验签 ID 和文件须同时配置')
        url = data.get('notify_url')
        if url and mode != 'channel':
            p = urlsplit(url)
            if p.scheme != 'https' or p.query or p.fragment or p.username:
                self.add_error('notify_url', '回调地址须为无查询参数的 HTTPS 地址')
            if p.path != f'/callbacks/{data.get("mch_id")}/':
                self.add_error('notify_url', '回调路径须为 /callbacks/商户号/')
        hosts = data.get('allowed_jump_hosts', '')
        if hosts and any(not re.fullmatch(r'[a-zA-Z0-9.-]+', h.strip()) for h in hosts.split(',')):
            self.add_error('allowed_jump_hosts', '请输入逗号分隔的域名，不包含协议和路径')
        return data


class ChannelForm(forms.ModelForm):
    api_v3_key_input = forms.CharField(label='APIv3 密钥', required=False,
        widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}, render_value=False),
        help_text='渠道账号的32字节密钥；编辑时留空保留')
    certificate_bundle = forms.FileField(label='上传 API 证书包 ZIP', required=False,
        widget=forms.ClearableFileInput(attrs={'accept': '.zip'}),
        help_text='兼容原有证书包导入')
    certificate_upload = forms.FileField(label='上传渠道 API 证书 PEM', required=False,
        widget=forms.ClearableFileInput(attrs={'accept': '.pem', 'data-certificate-target': 'serial_no'}),
        help_text='选择 apiclient_cert.pem 后立即填入证书序列号，保存时再次校验')
    private_key_upload = forms.FileField(label='上传渠道私钥 PEM', required=False,
        widget=forms.ClearableFileInput(attrs={'accept': '.pem'}),
        help_text='选择与证书配套的 apiclient_key.pem')
    public_key_upload = forms.FileField(label='上传微信公钥／平台证书 PEM', required=False,
        widget=forms.ClearableFileInput(attrs={'accept': '.pem', 'data-certificate-target': 'public_key_id',
                                             'data-allow-public-key': 'true'}),
        help_text='选择平台证书后立即填入序列号；pub_key.pem 不含公钥 ID，需另行填写 PUB_KEY_ID')
    secondary_key_upload = forms.FileField(label='上传备用验签文件 PEM', required=False,
        widget=forms.ClearableFileInput(attrs={'accept': '.pem', 'data-certificate-target': 'secondary_key_id',
                                             'data-allow-public-key': 'true'}))

    class Meta:
        model = WechatChannel
        fields = ['name', 'provider', 'mch_id', 'enabled', 'serial_no', 'private_key_path',
                  'api_v3_key_input', 'public_key_id', 'public_key_path', 'secondary_key_id',
                  'secondary_key_path', 'notify_url']
        help_texts = {'provider': '例如：随行付、远付宝', 'mch_id': '微信调用账号的渠道号',
                      'private_key_path': '服务端绝对路径；该渠道账号配套的私钥',
                      'notify_url': '保存后需在通知配置中查询并注册；保存不会修改微信配置'}
        widgets = {'private_key_path': forms.TextInput(attrs={'autocomplete': 'off'}),
                   'public_key_path': forms.TextInput(attrs={'autocomplete': 'off'}),
                   'secondary_key_path': forms.TextInput(attrs={'autocomplete': 'off'})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.saved_credential_files = saved_credential_files(self.instance)
        self.api_v3_configured = bool(self.instance.pk and self.instance.api_v3_key)
        self.credential_uploads = {}
        self.fields['api_v3_key_input'].help_text = (
            '当前密钥已保存，不回显；留空保留，填写新值则替换' if self.api_v3_configured else
            '尚未保存密钥；请输入32字节 APIv3 密钥')
        self.fields['serial_no'].help_text = '上传 apiclient_cert.pem 后自动读取；也可手动填写，不能填写 PUB_KEY_ID'

    @property
    def basic_fields(self):
        return [self[name] for name in ('name', 'provider', 'mch_id', 'enabled')]

    @property
    def server_path_fields(self):
        return [self[name] for name in ('private_key_path', 'public_key_path', 'secondary_key_path')]

    @property
    def has_server_path_errors(self):
        return any(name in self.errors for name in ('private_key_path', 'public_key_path', 'secondary_key_path'))

    def clean_mch_id(self):
        mch = self.cleaned_data['mch_id']
        if not re.fullmatch(r'\d{6,32}', mch): raise forms.ValidationError('请输入6至32位数字微信渠道号')
        if self.instance.pk and mch != self.instance.mch_id:
            raise forms.ValidationError('已保存渠道号不能变更，请新增渠道账号')
        return mch

    def clean(self):
        data = super().clean()
        self.credential_uploads = {}
        key = data.get('api_v3_key_input', '')
        if key and len(key.encode()) != 32: self.add_error('api_v3_key_input', 'APIv3 密钥必须为32字节')
        if key: self.instance.api_v3_key = key
        clean_credential_uploads(self, data)
        url = data.get('notify_url')
        if url:
            p = urlsplit(url)
            if (p.scheme != 'https' or p.query or p.fragment or p.username or
                p.path != f'/callbacks/channels/{data.get("mch_id")}/'):
                self.add_error('notify_url', '须为无查询参数的 HTTPS 地址，路径为 /callbacks/channels/微信渠道号/')
        return data


class SyncForm(forms.Form):
    begin = forms.DateField(label='开始日期', widget=forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'}))
    end = forms.DateField(label='结束日期', widget=forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'}))

    def clean(self):
        data = super().clean()
        if data.get('begin') and data.get('end'):
            if data['end'] < data['begin'] or (data['end'] - data['begin']).days > 365:
                raise forms.ValidationError('同步范围需为一年以内，开始日期不能晚于结束日期')
            if data['end'] > timezone.localdate(): raise forms.ValidationError('结束日期不能在未来')
        return data


class ReplyForm(forms.Form):
    operation_id = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)
    content = forms.CharField(label='回复内容', max_length=500, widget=forms.Textarea(attrs={'rows': 5, 'placeholder': '输入回复…'}))
    media = forms.JSONField(required=False, initial=list, widget=forms.HiddenInput)
    jump_url = forms.URLField(label='跳转链接', max_length=512, required=False)
    jump_url_text = forms.CharField(label='链接文案', max_length=10, required=False)
    mini_appid = forms.CharField(label='小程序 AppID', max_length=64, required=False)
    mini_path = forms.CharField(label='小程序页面路径', max_length=512, required=False)
    mini_text = forms.CharField(label='小程序页面名称', max_length=64, required=False)

    def __init__(self, *args, complaint, **kwargs):
        super().__init__(*args, **kwargs)
        self.complaint = complaint

    def clean_media(self):
        media = self.cleaned_data.get('media') or []
        if not isinstance(media, list) or len(media) > 4 or any(not isinstance(x, str) for x in media):
            raise forms.ValidationError('最多上传4张图片')
        if not set(media) <= allowed_media(self.complaint): raise forms.ValidationError('图片不属于此投诉或商户')
        return media

    def clean(self):
        data = super().clean()
        url, text = data.get('jump_url'), data.get('jump_url_text')
        if bool(url) != bool(text): raise forms.ValidationError('链接和文案须同时填写')
        if url: validate_jump(self.complaint.merchant, url)
        fields = [data.get(x) for x in ('mini_appid', 'mini_path', 'mini_text')]
        if any(fields) and not all(fields): raise forms.ValidationError('小程序 AppID、路径和名称须一起填写')
        return data

    def payload(self):
        data = self.cleaned_data
        body = {'complainted_mchid': self.complaint.merchant.mch_id, 'response_content': data['content']}
        if data['media']: body['response_images'] = data['media']
        if data['jump_url']: body.update(jump_url=data['jump_url'], jump_url_text=data['jump_url_text'])
        if data['mini_appid']: body['mini_program_jump_info'] = {'appid': data['mini_appid'], 'path': data['mini_path'], 'text': data['mini_text']}
        return body


def validate_jump(merchant, url):
    try:
        p = urlsplit(url)
        port = p.port
    except ValueError:
        raise forms.ValidationError('链接格式不正确')
    allowed = {x.strip().lower() for x in merchant.allowed_jump_hosts.split(',') if x.strip()}
    if p.scheme != 'https' or p.hostname not in allowed or p.username or port not in (None, 443):
        raise forms.ValidationError('链接须为商户已配置允许域名的 HTTPS 地址')


class RefundForm(forms.Form):
    operation_id = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)
    action = forms.ChoiceField(label='审批结果', choices=[('REJECT', '拒绝退款'), ('APPROVE', '同意退款审批')])
    launch_refund_day = forms.IntegerField(label='预计退款发起天数（工作日）', min_value=0, required=False)
    reject_reason = forms.CharField(label='拒绝原因', max_length=200, required=False, widget=forms.Textarea(attrs={'rows': 3}))
    media = forms.JSONField(required=False, initial=list, widget=forms.HiddenInput)
    remark = forms.CharField(label='客服备注', max_length=200, required=False, widget=forms.Textarea(attrs={'rows': 2}))
    acknowledged = forms.BooleanField(label='我已核实审批依据')

    def __init__(self, *args, complaint, **kwargs):
        super().__init__(*args, **kwargs)
        self.complaint = complaint

    def clean_media(self):
        return ReplyForm.clean_media(self)

    def clean(self):
        data = super().clean()
        if data.get('action') == 'REJECT' and not data.get('reject_reason'): self.add_error('reject_reason', '拒绝退款须提供原因')
        if data.get('action') == 'APPROVE' and data.get('launch_refund_day') is None:
            self.add_error('launch_refund_day', '同意审批须填写预计真实退款工作日数，0为当天')
        return data

    def payload(self):
        data = self.cleaned_data
        body = {'action': data['action']}
        if data['action'] == 'APPROVE': body['launch_refund_day'] = data['launch_refund_day']
        else:
            body['reject_reason'] = data['reject_reason']
            if data['media']: body['reject_media_list'] = data['media']
        if data['remark']: body['remark'] = data['remark']
        return body


class ImmediateForm(forms.Form):
    operation_id = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)
    content = forms.CharField(label='人工文本回复', max_length=500, required=False, widget=forms.Textarea(attrs={'rows': 4}))
    message = forms.JSONField(label='高级消息 JSON（优先使用）', required=False,
        widget=forms.Textarea(attrs={'rows': 10, 'placeholder': '{"blocks":[{"type":"TEXT","text":{"text":"您好"}}],"sender_identity":"MANUAL"}'}))

    def __init__(self, *args, complaint, **kwargs):
        super().__init__(*args, **kwargs)
        self.complaint = complaint

    def clean(self):
        data = super().clean()
        msg = data.get('message')
        if msg is None:
            if not data.get('content'): raise forms.ValidationError('请填写文本或高级消息')
            msg = {'blocks': [{'type': 'TEXT', 'text': {'text': data['content']}}], 'sender_identity': 'MANUAL'}
        if not isinstance(msg, dict) or not isinstance(msg.get('blocks'), list) or not 1 <= len(msg['blocks']) <= 20:
            raise forms.ValidationError('高级消息须为含1至20个 blocks 的对象')
        allowed = {'TEXT', 'IMAGE', 'LINK', 'FAQ_LIST', 'BUTTON', 'BUTTON_GROUP'}
        for block in msg['blocks']:
            if not isinstance(block, dict) or block.get('type') not in allowed:
                raise forms.ValidationError('消息块类型无效')
        def validate(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == 'jump_url':
                        if not isinstance(value, str): raise forms.ValidationError('跳转链接格式不正确')
                        validate_jump(self.complaint.merchant, value)
                    if key == 'media_id' and (not isinstance(value, str) or value not in allowed_media(self.complaint)):
                        raise forms.ValidationError('图片不属于此投诉')
                    validate(value)
            elif isinstance(node, list):
                for value in node: validate(value)
        validate(msg)
        msg['sender_identity'] = 'MANUAL'
        data['message'] = msg
        return data

    def payload(self):
        return {'complainted_mchid': self.complaint.merchant.mch_id, 'message': self.cleaned_data['message']}


class NotificationForm(forms.Form):
    operation_id = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)
    action = forms.ChoiceField(label='动作', choices=[('GET', '查询微信配置'), ('POST', '创建通知地址'), ('PUT', '更新通知地址'), ('DELETE', '删除通知地址（停止推送）')])
    url = forms.URLField(label='通知地址', max_length=512, required=False)
    confirmed = forms.BooleanField(label='我已核对通知地址', required=False)

    def __init__(self, *args, merchant, **kwargs):
        super().__init__(*args, **kwargs)
        self.merchant = merchant

    def clean(self):
        data = super().clean()
        if data.get('action') in ('POST', 'PUT'):
            p = urlsplit(data.get('url', ''))
            if p.scheme != 'https' or not p.netloc or p.query or p.fragment or p.username or p.path != self.merchant.callback_path:
                message = (f'须为公网 HTTPS 地址，路径为 {self.merchant.callback_path}，无查询参数'
                           if isinstance(self.merchant, WechatChannel) else
                           '须为公网 HTTPS 地址，路径为 /callbacks/当前商户号/，无查询参数')
                self.add_error('url', message)
        if data.get('action') != 'GET' and not data.get('confirmed'):
            self.add_error('confirmed', '请先核对现有回调地址用途')
        return data
